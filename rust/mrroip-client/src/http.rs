// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! The HTTP/1.1 an endpoint speaks: one request per connection, `Content-Length` or chunked bodies, no TLS.
//! Small enough that pulling in an HTTP stack for it would be the larger dependency.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::time::Duration;

/// Responses larger than this are refused: nothing an endpoint serves comes near it
const RESPONSE_MAX: usize = 16 * 1024 * 1024;

#[derive(Debug)]
pub struct Response {
    pub status: u16,
    /// Names lower-cased
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Response {
    pub fn header(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers.iter().find(|(k, _)| *k == name).map(|(_, v)| v.as_str())
    }
}

fn invalid(what: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, what.to_string())
}

/// One request to `host` (`ip` or `ip:port`). Returns whatever status the endpoint answered.
pub fn request(
    host: &str,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
    timeout: Duration,
) -> io::Result<Response> {
    let target = if host.contains(':') { host.to_string() } else { format!("{host}:80") };
    let addr: SocketAddr = target.to_socket_addrs()?.next().ok_or_else(|| invalid("no address"))?;
    let mut stream = TcpStream::connect_timeout(&addr, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    stream.set_nodelay(true)?;

    let mut head = format!("{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n");
    for (k, v) in headers {
        head.push_str(&format!("{k}: {v}\r\n"));
    }
    if !body.is_empty() || matches!(method, "POST" | "PUT") {
        head.push_str(&format!("Content-Length: {}\r\n", body.len()));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes())?;
    stream.write_all(body)?;
    stream.flush()?;

    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    reader.read_line(&mut line)?;
    let status: u16 = line.split_whitespace().nth(1).and_then(|s| s.parse().ok()).ok_or_else(|| invalid("bad status line"))?;

    let mut response_headers = Vec::new();
    loop {
        line.clear();
        if reader.read_line(&mut line)? == 0 {
            break;
        }
        let trimmed = line.trim_end();
        if trimmed.is_empty() {
            break;
        }
        if let Some((k, v)) = trimmed.split_once(':') {
            response_headers.push((k.trim().to_ascii_lowercase(), v.trim().to_string()));
        }
    }
    let find = |name: &str| response_headers.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone());

    let mut body = Vec::new();
    if method == "HEAD" || status == 204 || status == 304 {
        // no body
    } else if find("transfer-encoding").is_some_and(|v| v.eq_ignore_ascii_case("chunked")) {
        loop {
            line.clear();
            reader.read_line(&mut line)?;
            let size = usize::from_str_radix(line.trim().split(';').next().unwrap_or(""), 16)
                .map_err(|_| invalid("bad chunk size"))?;
            if size == 0 {
                break;
            }
            if body.len() + size > RESPONSE_MAX {
                return Err(invalid("response too large"));
            }
            let start = body.len();
            body.resize(start + size, 0);
            reader.read_exact(&mut body[start..])?;
            line.clear();
            reader.read_line(&mut line)?;
        }
    } else if let Some(length) = find("content-length") {
        let length: usize = length.parse().map_err(|_| invalid("bad Content-Length"))?;
        if length > RESPONSE_MAX {
            return Err(invalid("response too large"));
        }
        body.resize(length, 0);
        reader.read_exact(&mut body)?;
    } else {
        reader.take(RESPONSE_MAX as u64).read_to_end(&mut body)?;
    }
    Ok(Response { status, headers: response_headers, body })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    fn serve_once(reply: &'static [u8]) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 2048];
            let _ = s.read(&mut buf);
            s.write_all(reply).unwrap();
        });
        addr.to_string()
    }

    #[test]
    fn content_length() {
        let host = serve_once(b"HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nContent-Length: 7\r\n\r\n{\"a\":1}");
        let r = request(&host, "POST", "/config", &[], b"{}", Duration::from_secs(2)).unwrap();
        assert_eq!(r.status, 409);
        assert_eq!(r.header("Content-Type"), Some("application/json"));
        assert_eq!(r.body, b"{\"a\":1}");
    }

    #[test]
    fn chunked() {
        let host = serve_once(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\n{\"a\r\n4\r\n\":1}\r\n0\r\n\r\n");
        let r = request(&host, "GET", "/state", &[], b"", Duration::from_secs(2)).unwrap();
        assert_eq!(r.body, b"{\"a\":1}");
    }
}
