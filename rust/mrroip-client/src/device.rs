// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! One endpoint over HTTP and UDP (MRROIP-1.md §7–§9.8). Device-agnostic: what the endpoint contains and
//! accepts comes from its `/definition`.

use std::fmt;
use std::net::{Ipv4Addr, UdpSocket};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use mrroip_proto::protocol::{HEADER_PREFIX, HTTP_PORT, UDP_PORT};
use mrroip_proto::{ConfigReply, ControlMessage, ControlResponse, Definition, ErrorBody, State};
use serde::de::DeserializeOwned;
use serde_json::{Map, Value};

use crate::http;

#[derive(Debug)]
pub enum Error {
    Io(std::io::Error),
    /// The endpoint answered with something that is not the document asked for
    Protocol { status: u16, message: String },
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Io(e) => write!(f, "{e}"),
            Error::Protocol { status, message } => write!(f, "HTTP {status}: {message}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;

/// What a `POST /config` came to (§8.2)
#[derive(Clone, Debug)]
pub enum ConfigOutcome {
    Applied(ConfigReply),
    /// `400`: nothing applied; `details` names every refused key
    Refused(ErrorBody),
    /// `409 version_conflict`: the current config, to merge against and retry
    Conflict(ConfigReply),
}

/// One endpoint. Cheap to clone; clones share the `seq` counter, which must grow per sender address (§9.5)
/// across HTTP, UDP and uploads alike.
#[derive(Clone)]
pub struct Device {
    pub ip: Ipv4Addr,
    pub http_port: u16,
    pub udp_port: u16,
    pub timeout: Duration,
    seq: Arc<AtomicU64>,
    started: Instant,
    cached: Arc<Mutex<Option<(String, Definition)>>>,
}

impl Device {
    /// `addr` is `ip` or `ip:port` (§5.3)
    pub fn new(addr: &str) -> Result<Device> {
        let (ip, port) = match addr.split_once(':') {
            Some((ip, port)) => (ip, port.parse().map_err(|_| protocol(0, "bad port"))?),
            None => (addr, HTTP_PORT),
        };
        let ip: Ipv4Addr = ip.parse().map_err(|_| protocol(0, "not an IPv4 address"))?;
        Ok(Device::at(ip, port))
    }

    pub fn at(ip: Ipv4Addr, http_port: u16) -> Device {
        // Counting from a millisecond clock keeps several programs on one host in order
        let seq = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(1);
        Device {
            ip,
            http_port,
            udp_port: UDP_PORT,
            timeout: Duration::from_secs(4),
            seq: Arc::new(AtomicU64::new(seq)),
            started: Instant::now(),
            cached: Arc::new(Mutex::new(None)),
        }
    }

    /// `ip`, or `ip:port` when HTTP is not on 80
    pub fn host(&self) -> String {
        if self.http_port == HTTP_PORT { self.ip.to_string() } else { format!("{}:{}", self.ip, self.http_port) }
    }

    pub fn next_seq(&self) -> u64 {
        self.seq.fetch_add(1, Ordering::Relaxed) + 1
    }

    fn ts(&self) -> u64 {
        self.started.elapsed().as_millis() as u64
    }

    // -- HTTP ---------------------------------------------------------------------------------------------

    /// Any path: status and raw body, e.g. for an icon
    pub fn get_raw(&self, path: &str) -> Result<http::Response> {
        Ok(http::request(&self.host(), "GET", path, &[], b"", self.timeout)?)
    }

    fn json<T: DeserializeOwned>(&self, method: &str, path: &str, body: Option<&Value>) -> Result<(u16, T)> {
        let bytes = body.map(|b| serde_json::to_vec(b).expect("JSON")).unwrap_or_default();
        let headers: &[(&str, &str)] = if body.is_some() { &[("Content-Type", "application/json")] } else { &[] };
        let r = http::request(&self.host(), method, path, headers, &bytes, self.timeout)?;
        let value = serde_json::from_slice(&r.body).map_err(|e| protocol(r.status, &format!("{path}: {e}")))?;
        Ok((r.status, value))
    }

    /// `GET /definition`, revalidated with its ETag (§7)
    pub fn definition(&self) -> Result<Definition> {
        let cached = self.cached.lock().unwrap().clone();
        let etag = cached.as_ref().map(|(tag, _)| tag.clone());
        let headers: Vec<(&str, &str)> = etag.iter().map(|t| ("If-None-Match", t.as_str())).collect();
        let r = http::request(&self.host(), "GET", "/definition", &headers, b"", self.timeout)?;
        if r.status == 304
            && let Some((_, d)) = cached {
                return Ok(d);
            }
        if r.status != 200 {
            return Err(protocol(r.status, "/definition"));
        }
        let d: Definition = serde_json::from_slice(&r.body).map_err(|e| protocol(r.status, &format!("/definition: {e}")))?;
        if let Some(tag) = r.header("etag") {
            *self.cached.lock().unwrap() = Some((tag.to_string(), d.clone()));
        }
        Ok(d)
    }

    pub fn config(&self) -> Result<ConfigReply> {
        let (status, reply) = self.json("GET", "/config", None)?;
        expect_ok(status, "/config")?;
        Ok(reply)
    }

    /// `POST /config` (§8.2). `persist` stores the values; without it they apply until the next restart.
    pub fn set_config(&self, values: &Map<String, Value>, persist: bool, if_version: Option<u64>) -> Result<ConfigOutcome> {
        let mut body = values.clone();
        if persist {
            body.insert("persist".into(), Value::Bool(true));
        }
        if let Some(v) = if_version {
            body.insert("if_version".into(), v.into());
        }
        let (status, reply): (u16, Value) = self.json("POST", "/config", Some(&Value::Object(body)))?;
        fn parse<T: DeserializeOwned>(status: u16, v: Value) -> Result<T> {
            serde_json::from_value(v).map_err(|e| protocol(status, &format!("/config: {e}")))
        }
        match status {
            200 => Ok(ConfigOutcome::Applied(parse(status, reply)?)),
            409 => Ok(ConfigOutcome::Conflict(parse(status, reply)?)),
            _ => Ok(ConfigOutcome::Refused(parse(status, reply)?)),
        }
    }

    /// Erases every stored value and restarts the endpoint (§8.2)
    pub fn factory_reset(&self) -> Result<()> {
        let body = serde_json::json!({"factory_reset": true});
        let (status, _): (u16, Value) = self.json("POST", "/config", Some(&body))?;
        expect_ok(status, "factory_reset")
    }

    pub fn state(&self) -> Result<State> {
        let (status, state) = self.json("GET", "/state", None)?;
        expect_ok(status, "/state")?;
        Ok(state)
    }

    fn stamp(&self, mut msg: ControlMessage) -> ControlMessage {
        if msg.seq == 0 {
            msg.seq = self.next_seq();
        }
        msg.ts.get_or_insert_with(|| self.ts());
        msg
    }

    /// `POST /control`: the HTTP status and the response, accepted or not (§9.4)
    pub fn control(&self, msg: ControlMessage) -> Result<(u16, ControlResponse)> {
        let body = serde_json::to_value(self.stamp(msg)).expect("JSON");
        self.json("POST", "/control", Some(&body))
    }

    /// The same message as one UDP datagram, waiting for the reply
    pub fn control_udp(&self, msg: ControlMessage) -> Result<ControlResponse> {
        let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0))?;
        self.control_udp_on(&socket, msg)
    }

    /// Over a socket the caller keeps, e.g. for a stream of setpoints
    pub fn control_udp_on(&self, socket: &UdpSocket, msg: ControlMessage) -> Result<ControlResponse> {
        let data = serde_json::to_vec(&self.stamp(msg)).expect("JSON");
        socket.set_read_timeout(Some(self.timeout))?;
        socket.send_to(&data, (self.ip, self.udp_port))?;
        let mut buf = [0u8; 4096];
        loop {
            let (n, from) = socket.recv_from(&mut buf)?;
            if from.ip() == self.ip {
                return serde_json::from_slice(&buf[..n]).map_err(|e| protocol(0, &format!("UDP reply: {e}")));
            }
        }
    }

    /// `PUT /objects/<id>`: a binary object state (§9.8)
    pub fn put_object(&self, id: &str, data: &[u8], query: &[(&str, String)], base: Option<&str>) -> Result<(u16, ControlResponse)> {
        let seq = self.next_seq().to_string();
        let seq_header = format!("{HEADER_PREFIX}Seq");
        let base_header = format!("{HEADER_PREFIX}Base");
        let mut headers = vec![("Content-Type", "application/octet-stream"), (seq_header.as_str(), seq.as_str())];
        if let Some(base) = base {
            headers.push((base_header.as_str(), base));
        }
        let q: Vec<String> = query.iter().map(|(k, v)| format!("{k}={v}")).collect();
        let path = if q.is_empty() { format!("/objects/{id}") } else { format!("/objects/{id}?{}", q.join("&")) };
        let r = http::request(&self.host(), "PUT", &path, &headers, data, self.timeout)?;
        let reply = serde_json::from_slice(&r.body).map_err(|e| protocol(r.status, &format!("{path}: {e}")))?;
        Ok((r.status, reply))
    }
}

fn protocol(status: u16, message: &str) -> Error {
    Error::Protocol { status, message: message.to_string() }
}

fn expect_ok(status: u16, what: &str) -> Result<()> {
    if status == 200 { Ok(()) } else { Err(protocol(status, what)) }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn addresses() {
        let d = Device::new("10.0.0.5").unwrap();
        assert_eq!((d.http_port, d.host()), (80, "10.0.0.5".to_string()));
        let d = Device::new("10.0.0.5:8080").unwrap();
        assert_eq!((d.http_port, d.host()), (8080, "10.0.0.5:8080".to_string()));
        assert!(Device::new("host.local").is_err());
    }

    #[test]
    fn seq_is_shared_by_clones() {
        let d = Device::new("10.0.0.5").unwrap();
        let e = d.clone();
        let a = d.next_seq();
        assert_eq!(e.next_seq(), a + 1);
    }
}
