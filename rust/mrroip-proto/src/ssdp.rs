// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! SSDP messages (MRROIP-1.md §6.1): parsing for masters, building for endpoints. The layout follows the C
//! core's `discovery.c` line for line, so every implementation announces itself identically.

use std::collections::BTreeMap;

use crate::protocol::{HEADER_PREFIX, NAME, SSDP_ADDR, SSDP_PORT, SSDP_ST, TOKEN, VERSION, usn};

/// `max-age` of every announcement
pub const MAX_AGE_S: u32 = 600;

/// One SSDP message: its first line and its headers, names upper-cased
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Message {
    pub first_line: String,
    pub headers: BTreeMap<String, String>,
}

impl Message {
    pub fn parse(data: &[u8]) -> Message {
        let text = String::from_utf8_lossy(data);
        let mut lines = text.split("\r\n");
        let first_line = lines.next().unwrap_or("").to_string();
        let headers = lines
            .filter_map(|line| line.split_once(':'))
            .map(|(k, v)| (k.trim().to_ascii_uppercase(), v.trim().to_string()))
            .collect();
        Message { first_line, headers }
    }

    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.get(&name.to_ascii_uppercase()).map(String::as_str)
    }

    /// `X-MRROIP-<suffix>`
    pub fn mrroip(&self, suffix: &str) -> Option<&str> {
        self.header(&format!("{HEADER_PREFIX}{suffix}"))
    }

    pub fn is_notify(&self) -> bool {
        self.first_line.starts_with("NOTIFY")
    }

    pub fn is_msearch(&self) -> bool {
        self.first_line.starts_with("M-SEARCH")
    }

    /// Whether an endpoint of this protocol sent it
    pub fn is_mrroip(&self) -> bool {
        ["USN", "ST", "NT"].iter().any(|h| self.header(h).is_some_and(|v| v.to_ascii_lowercase().contains(TOKEN)))
            || self.mrroip("ID").is_some()
    }
}

/// An M-SEARCH for `st`, answered within `mx` seconds
pub fn msearch(st: &str, mx: u32) -> String {
    let [a, b, c, d] = SSDP_ADDR;
    format!("M-SEARCH * HTTP/1.1\r\nHOST: {a}.{b}.{c}.{d}:{SSDP_PORT}\r\nMAN: \"ssdp:discover\"\r\nMX: {mx}\r\nST: {st}\r\n\r\n")
}

/// Whether an M-SEARCH asks for us: `ssdp:all` or the MRRoIP target
pub fn search_wants_us(m: &Message) -> bool {
    m.is_msearch() && m.header("ST").is_some_and(|st| st == "ssdp:all" || st == SSDP_ST)
}

/// The MX of an M-SEARCH, clamped to 1–5 s as the C core does
pub fn search_mx(m: &Message) -> u32 {
    m.header("MX").and_then(|v| v.parse().ok()).unwrap_or(1).clamp(1, 5)
}

/// What an endpoint says about itself in `ssdp:alive` and in a search response
#[derive(Clone, Debug)]
pub struct Announcement<'a> {
    /// `ip`, or `ip:port` for an endpoint not serving HTTP on 80 (§5.3)
    pub host: &'a str,
    pub device_id: &'a str,
    pub device_name: &'a str,
    pub device_type: &'a str,
    pub device_class: &'a str,
    /// The platform part of `SERVER`, e.g. `esp-idf/6.1` or `linux/6.6`
    pub platform: &'a str,
}

enum Kind {
    Alive,
    Response,
}

fn header_safe(s: &str) -> String {
    s.chars().map(|c| if (c as u32) < 0x20 { '?' } else { c }).collect()
}

fn describe(a: &Announcement, kind: Kind) -> String {
    let [g0, g1, g2, g3] = SSDP_ADDR;
    let (start, target, nts) = match kind {
        Kind::Alive => (format!("NOTIFY * HTTP/1.1\r\nHOST: {g0}.{g1}.{g2}.{g3}:{SSDP_PORT}\r\n"), "NT", "NTS: ssdp:alive\r\n"),
        Kind::Response => ("HTTP/1.1 200 OK\r\nEXT:\r\n".to_string(), "ST", ""),
    };
    format!(
        "{start}CACHE-CONTROL: max-age={MAX_AGE_S}\r\nLOCATION: http://{host}/definition\r\n{target}: {SSDP_ST}\r\n{nts}\
         USN: {usn}\r\nSERVER: {platform} {NAME}/{VERSION}\r\n\
         {p}ID: {id}\r\n{p}NAME: {name}\r\n{p}TYPE: {typ}\r\n{p}CLASS: {class}\r\n\r\n",
        host = a.host,
        usn = usn(a.device_id),
        platform = a.platform,
        p = HEADER_PREFIX,
        id = a.device_id,
        name = header_safe(a.device_name),
        typ = a.device_type,
        class = a.device_class,
    )
}

pub fn notify_alive(a: &Announcement) -> String {
    describe(a, Kind::Alive)
}

pub fn search_response(a: &Announcement) -> String {
    describe(a, Kind::Response)
}

pub fn notify_byebye(device_id: &str) -> String {
    let [a, b, c, d] = SSDP_ADDR;
    format!(
        "NOTIFY * HTTP/1.1\r\nHOST: {a}.{b}.{c}.{d}:{SSDP_PORT}\r\nNT: {SSDP_ST}\r\nNTS: ssdp:byebye\r\nUSN: {}\r\n\r\n",
        usn(device_id)
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn announcement() -> Announcement<'static> {
        Announcement {
            host: "192.168.1.47",
            device_id: "a0:b7:65:12:34:56",
            device_name: "drehscheibe",
            device_type: "turntable",
            device_class: "stationary",
            platform: "esp-idf/5.2",
        }
    }

    #[test]
    fn alive_matches_the_spec_example() {
        let expected = "NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nCACHE-CONTROL: max-age=600\r\n\
            LOCATION: http://192.168.1.47/definition\r\nNT: urn:schemas-mrroip-org:device:Endpoint:1\r\n\
            NTS: ssdp:alive\r\nUSN: uuid:mrroip-a0b765123456::urn:schemas-mrroip-org:device:Endpoint:1\r\n\
            SERVER: esp-idf/5.2 MRRoIP/0.1\r\nX-MRROIP-ID: a0:b7:65:12:34:56\r\nX-MRROIP-NAME: drehscheibe\r\n\
            X-MRROIP-TYPE: turntable\r\nX-MRROIP-CLASS: stationary\r\n\r\n";
        assert_eq!(notify_alive(&announcement()), expected);
    }

    #[test]
    fn parse_what_we_build() {
        let m = Message::parse(search_response(&announcement()).as_bytes());
        assert_eq!(m.first_line, "HTTP/1.1 200 OK");
        assert!(m.is_mrroip());
        assert_eq!(m.header("st"), Some(SSDP_ST));
        assert_eq!(m.mrroip("NAME"), Some("drehscheibe"));
        let bye = Message::parse(notify_byebye("a0:b7:65:12:34:56").as_bytes());
        assert!(bye.is_notify() && bye.is_mrroip());
        assert_eq!(bye.header("NTS"), Some("ssdp:byebye"));
    }

    #[test]
    fn searches() {
        let m = Message::parse(msearch("ssdp:all", 9).as_bytes());
        assert!(search_wants_us(&m));
        assert_eq!(search_mx(&m), 5);
        let other = Message::parse(msearch("urn:other", 2).as_bytes());
        assert!(!search_wants_us(&other));
    }
}
