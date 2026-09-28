// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! Finding endpoints (MRROIP-1.md §6): SSDP, which is normative, the whois probe, a diagnostic for networks
//! that block multicast, and mDNS, a convenience. The same three as `mrroip.discovery` in Python, but each
//! reports what it finds as it arrives, so a user interface never waits for a search to end.
//!
//! On a computer with several networks, multicast and broadcast leave through the default interface. Pass
//! `iface` (a local IPv4 address) to use another one; `MRROIP_IFACE` sets the default.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::io;
use std::mem::MaybeUninit;
use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use mrroip_proto::protocol::{self, MDNS_ADDR, MDNS_PORT, SSDP_ADDR, SSDP_PORT, SSDP_ST, WHOIS_PORT};
use mrroip_proto::ssdp::{self, Message};
use mrroip_proto::WhoisReply;
use socket2::{Domain, Protocol, SockAddr, Socket, Type};

/// How an endpoint was found
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Via {
    /// Answered an M-SEARCH
    Search,
    /// Announced itself with NOTIFY
    Notify,
    Whois,
    Mdns,
}

/// One sighting of an endpoint
#[derive(Clone, Debug, PartialEq)]
pub struct Found {
    pub via: Via,
    pub ip: Ipv4Addr,
    pub http_port: u16,
    /// Absent only where the source does not carry it
    pub device_id: Option<String>,
    pub name: Option<String>,
    pub device_type: Option<String>,
    pub device_class: Option<String>,
    /// `false` for `ssdp:byebye`
    pub alive: bool,
    /// How long the sighting stays valid, from `CACHE-CONTROL: max-age`
    pub max_age: Option<Duration>,
}

impl Found {
    fn from_ssdp(via: Via, ip: Ipv4Addr, m: &Message) -> Found {
        let http_port = m
            .header("LOCATION")
            .and_then(|loc| loc.strip_prefix("http://"))
            .and_then(|rest| rest.split('/').next())
            .and_then(|hostport| hostport.rsplit_once(':'))
            .and_then(|(_, port)| port.parse().ok())
            .unwrap_or(protocol::HTTP_PORT);
        let max_age = m
            .header("CACHE-CONTROL")
            .and_then(|v| v.split("max-age=").nth(1))
            .and_then(|v| v.trim().parse().ok())
            .map(Duration::from_secs);
        let device_id = m.mrroip("ID").map(str::to_string).or_else(|| m.header("USN").and_then(protocol::device_id_from_usn));
        Found {
            via,
            ip,
            http_port,
            device_id,
            name: m.mrroip("NAME").map(str::to_string),
            device_type: m.mrroip("TYPE").map(str::to_string),
            device_class: m.mrroip("CLASS").map(str::to_string),
            alive: m.header("NTS") != Some("ssdp:byebye"),
            max_age,
        }
    }
}

/// `MRROIP_IFACE`, if set to an IPv4 address
pub fn default_iface() -> Option<Ipv4Addr> {
    std::env::var("MRROIP_IFACE").ok()?.parse().ok()
}

fn recv(socket: &Socket, buf: &mut [u8]) -> io::Result<(usize, Ipv4Addr)> {
    // socket2 reads into MaybeUninit; the buffer is initialised already
    let uninit = unsafe { &mut *(buf as *mut [u8] as *mut [MaybeUninit<u8>]) };
    let (n, from) = socket.recv_from(uninit)?;
    let ip = from.as_socket_ipv4().map(|a| *a.ip()).ok_or_else(|| io::Error::other("not IPv4"))?;
    Ok((n, ip))
}

fn udp_socket(iface: Option<Ipv4Addr>, ttl: u32) -> io::Result<Socket> {
    let s = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
    s.set_reuse_address(true)?;
    s.set_multicast_ttl_v4(ttl)?;
    s.set_broadcast(true)?;
    if let Some(ip) = iface {
        s.set_multicast_if_v4(&ip)?;
    }
    // answers come back to this interface
    s.bind(&SockAddr::from(SocketAddrV4::new(iface.unwrap_or(Ipv4Addr::UNSPECIFIED), 0)))?;
    s.set_read_timeout(Some(Duration::from_millis(200)))?;
    Ok(s)
}

fn timed_out(e: &io::Error) -> bool {
    matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut)
}

/// Sends an M-SEARCH for the MRRoIP target and reports every endpoint that answers, until `timeout`.
pub fn ssdp_search(timeout: Duration, iface: Option<Ipv4Addr>, found: &mut dyn FnMut(Found)) -> io::Result<()> {
    let iface = iface.or_else(default_iface);
    let s = udp_socket(iface, 4)?;
    let group = SockAddr::from(SocketAddrV4::new(SSDP_ADDR.into(), SSDP_PORT));
    let msg = ssdp::msearch(SSDP_ST, 2);
    for _ in 0..3 {
        s.send_to(msg.as_bytes(), &group)?;
        thread::sleep(Duration::from_millis(150));
    }
    let t0 = Instant::now();
    let mut buf = [0u8; 2048];
    while t0.elapsed() < timeout {
        match recv(&s, &mut buf) {
            Ok((n, ip)) => {
                let m = Message::parse(&buf[..n]);
                if m.is_mrroip() && !m.is_msearch() {
                    found(Found::from_ssdp(Via::Search, ip, &m));
                }
            }
            Err(e) if timed_out(&e) => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// Reports the NOTIFY messages endpoints send by themselves (`ssdp:alive`, `ssdp:byebye`) from a background
/// thread, until stopped or dropped.
pub struct NotifyListener {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl NotifyListener {
    pub fn start(iface: Option<Ipv4Addr>, mut found: impl FnMut(Found) + Send + 'static) -> io::Result<NotifyListener> {
        let iface = iface.or_else(default_iface);
        let s = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
        s.set_reuse_address(true)?;
        #[cfg(unix)]
        s.set_reuse_port(true)?; // other SSDP listeners may hold the port
        s.bind(&SockAddr::from(SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, SSDP_PORT)))?;
        s.join_multicast_v4(&SSDP_ADDR.into(), &iface.unwrap_or(Ipv4Addr::UNSPECIFIED))?;
        s.set_read_timeout(Some(Duration::from_millis(200)))?;
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let thread = thread::Builder::new().name("ssdp-notify".into()).spawn(move || {
            let mut buf = [0u8; 4096];
            while !flag.load(Ordering::Relaxed) {
                match recv(&s, &mut buf) {
                    Ok((n, ip)) => {
                        let m = Message::parse(&buf[..n]);
                        if m.is_notify() && m.is_mrroip() {
                            found(Found::from_ssdp(Via::Notify, ip, &m));
                        }
                    }
                    Err(e) if timed_out(&e) => {}
                    Err(_) => break,
                }
            }
        })?;
        Ok(NotifyListener { stop, thread: Some(thread) })
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

impl Drop for NotifyListener {
    fn drop(&mut self) {
        self.stop();
    }
}

/// The whois probe (§6.3): unicast to `target`, or broadcast. Reports every reply until `timeout`.
pub fn whois(target: Option<Ipv4Addr>, timeout: Duration, iface: Option<Ipv4Addr>, found: &mut dyn FnMut(Found)) -> io::Result<()> {
    let iface = iface.or_else(default_iface);
    let s = udp_socket(iface, 1)?;
    let to = SockAddr::from(SocketAddrV4::new(target.unwrap_or(Ipv4Addr::BROADCAST), WHOIS_PORT));
    for _ in 0..3 {
        s.send_to(br#"{"m":"whois"}"#, &to)?;
        thread::sleep(Duration::from_millis(100));
    }
    let t0 = Instant::now();
    let mut buf = [0u8; 2048];
    let mut seen = HashSet::new();
    while t0.elapsed() < timeout {
        match recv(&s, &mut buf) {
            Ok((n, ip)) => {
                let Ok(reply) = serde_json::from_slice::<WhoisReply>(&buf[..n]) else { continue };
                if !seen.insert(ip) {
                    continue;
                }
                // off port 80, `definition` is a URL carrying the port (§5.3)
                let http_port = reply
                    .definition
                    .strip_prefix("http://")
                    .and_then(|rest| rest.split('/').next())
                    .and_then(|hostport| hostport.rsplit_once(':'))
                    .and_then(|(_, p)| p.parse().ok())
                    .unwrap_or(protocol::HTTP_PORT);
                found(Found {
                    via: Via::Whois,
                    ip,
                    http_port,
                    device_id: Some(reply.id),
                    name: Some(reply.name),
                    device_type: Some(reply.type_),
                    device_class: Some(reply.class),
                    alive: true,
                    max_age: None,
                });
            }
            Err(e) if timed_out(&e) => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

// ---------------------------------------------------------------------------------------- mDNS (RFC 6762/6763)

const PTR: u16 = 12;
const TXT: u16 = 16;
const SRV: u16 = 33;
const A: u16 = 1;

fn encode_name(name: &str) -> Vec<u8> {
    let mut out = Vec::new();
    for label in name.trim_end_matches('.').split('.') {
        out.push(label.len() as u8);
        out.extend_from_slice(label.as_bytes());
    }
    out.push(0);
    out
}

/// (name, position after it), following compression pointers
fn read_name(msg: &[u8], mut pos: usize) -> Option<(String, usize)> {
    let mut labels = Vec::new();
    let mut end = None;
    for _ in 0..64 {
        let len = *msg.get(pos)? as usize;
        if len & 0xC0 == 0xC0 {
            end.get_or_insert(pos + 2);
            pos = ((len & 0x3F) << 8) | *msg.get(pos + 1)? as usize;
            continue;
        }
        if len == 0 {
            return Some((labels.join("."), end.unwrap_or(pos + 1)));
        }
        labels.push(String::from_utf8_lossy(msg.get(pos + 1..pos + 1 + len)?).into_owned());
        pos += 1 + len;
    }
    None
}

fn u16_at(msg: &[u8], pos: usize) -> Option<u16> {
    Some(u16::from_be_bytes([*msg.get(pos)?, *msg.get(pos + 1)?]))
}

/// Every resource record of a DNS message: (lower-cased name, type, rdata start, rdata length)
fn records(msg: &[u8]) -> Option<Vec<(String, u16, usize, usize)>> {
    let qd = u16_at(msg, 4)?;
    let rr = u16_at(msg, 6)? as usize + u16_at(msg, 8)? as usize + u16_at(msg, 10)? as usize;
    let mut pos = 12;
    for _ in 0..qd {
        pos = read_name(msg, pos)?.1 + 4;
    }
    let mut out = Vec::new();
    for _ in 0..rr {
        let (name, p) = read_name(msg, pos)?;
        let rtype = u16_at(msg, p)?;
        let rdlen = u16_at(msg, p + 8)? as usize;
        let start = p + 10;
        if start + rdlen > msg.len() {
            return None;
        }
        out.push((name.to_lowercase(), rtype, start, rdlen));
        pos = start + rdlen;
    }
    Some(out)
}

/// Browses for `_mrroip._tcp` with one-shot queries from an ordinary port, so responders answer by unicast
/// (RFC 6762 §6.7) and nothing competes with the system's own responder for 5353. Reports at `timeout`.
pub fn mdns_browse(timeout: Duration, iface: Option<Ipv4Addr>, found: &mut dyn FnMut(Found)) -> io::Result<()> {
    let iface = iface.or_else(default_iface);
    let service = format!("{}._tcp.local", protocol::MDNS_SERVICE);
    let mut query = vec![0x4d, 0x52, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0];
    query.extend(encode_name(&service));
    query.extend([0, PTR as u8, 0, 1]);
    let s = udp_socket(iface, 255)?;
    let group = SockAddr::from(SocketAddrV4::new(MDNS_ADDR.into(), MDNS_PORT));

    let mut instances: BTreeMap<String, Ipv4Addr> = BTreeMap::new();
    let mut srv: HashMap<String, (String, u16)> = HashMap::new();
    let mut txt: HashMap<String, HashMap<String, String>> = HashMap::new();
    let mut addrs: HashMap<String, Ipv4Addr> = HashMap::new();
    s.send_to(&query, &group)?;
    let (t0, mut resent) = (Instant::now(), false);
    let mut buf = [0u8; 9000];
    while t0.elapsed() < timeout {
        if !resent && t0.elapsed() > timeout / 3 {
            s.send_to(&query, &group)?; // multicast is lossy
            resent = true;
        }
        let (n, from) = match recv(&s, &mut buf) {
            Ok(r) => r,
            Err(e) if timed_out(&e) => continue,
            Err(e) => return Err(e),
        };
        let msg = &buf[..n];
        for (name, rtype, pos, rdlen) in records(msg).unwrap_or_default() {
            match rtype {
                PTR if name == service => {
                    if let Some((instance, _)) = read_name(msg, pos) {
                        instances.insert(instance.to_lowercase(), from);
                    }
                }
                SRV if rdlen >= 6 => {
                    if let (Some(port), Some((target, _))) = (u16_at(msg, pos + 4), read_name(msg, pos + 6)) {
                        srv.insert(name, (target.to_lowercase(), port));
                    }
                }
                TXT => {
                    let (mut items, mut p) = (HashMap::new(), pos);
                    while p < pos + rdlen {
                        let len = msg[p] as usize;
                        let entry = String::from_utf8_lossy(&msg[p + 1..(p + 1 + len).min(pos + rdlen)]).into_owned();
                        if let Some((k, v)) = entry.split_once('=') {
                            items.insert(k.to_string(), v.to_string());
                        }
                        p += 1 + len;
                    }
                    txt.insert(name, items);
                }
                A if rdlen == 4 => {
                    addrs.insert(name, Ipv4Addr::new(msg[pos], msg[pos + 1], msg[pos + 2], msg[pos + 3]));
                }
                _ => {}
            }
        }
    }
    for (instance, source) in instances {
        let (host, port) = srv.get(&instance).cloned().unwrap_or_default();
        let t = txt.remove(&instance).unwrap_or_default();
        found(Found {
            via: Via::Mdns,
            ip: addrs.get(&host).copied().unwrap_or(source),
            http_port: if port == 0 { protocol::HTTP_PORT } else { port },
            device_id: t.get("id").cloned(),
            name: t.get("name").cloned(),
            device_type: t.get("type").cloned(),
            device_class: t.get("class").cloned(),
            alive: true,
            max_age: None,
        });
    }
    Ok(())
}

/// The local IPv4 address that faces `peer`: what an interface picker offers, and what an endpoint on this
/// host puts in `LOCATION`. No packet is sent.
pub fn local_ip_facing(peer: Ipv4Addr) -> io::Result<Ipv4Addr> {
    let s = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0))?;
    s.connect((peer, 9))?;
    match s.local_addr()? {
        SocketAddr::V4(a) => Ok(*a.ip()),
        SocketAddr::V6(_) => Err(io::Error::other("not IPv4")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ssdp_sighting() {
        let m = Message::parse(
            b"NOTIFY * HTTP/1.1\r\nCACHE-CONTROL: max-age=600\r\nLOCATION: http://10.0.0.9:8080/definition\r\n\
              NTS: ssdp:alive\r\nUSN: uuid:mrroip-a0b765123456::x\r\nX-MRROIP-ID: a0:b7:65:12:34:56\r\n\
              X-MRROIP-NAME: lamp\r\n\r\n",
        );
        let f = Found::from_ssdp(Via::Notify, Ipv4Addr::new(10, 0, 0, 9), &m);
        assert_eq!((f.http_port, f.alive, f.max_age), (8080, true, Some(Duration::from_secs(600))));
        assert_eq!(f.name.as_deref(), Some("lamp"));
        let bye = Message::parse(b"NOTIFY * HTTP/1.1\r\nNTS: ssdp:byebye\r\nUSN: uuid:mrroip-a0b765123456::x\r\n\r\n");
        let f = Found::from_ssdp(Via::Notify, Ipv4Addr::new(10, 0, 0, 9), &bye);
        assert_eq!((f.alive, f.device_id.as_deref()), (false, Some("a0:b7:65:12:34:56")));
    }

    #[test]
    fn dns_names_with_pointers() {
        let mut msg = vec![0u8; 12];
        msg.extend(encode_name("_mrroip._tcp.local"));
        let pointer_at = msg.len();
        msg.extend([4, b'l', b'a', b'm', b'p', 0xC0, 12]);
        assert_eq!(read_name(&msg, 12).unwrap().0, "_mrroip._tcp.local");
        assert_eq!(read_name(&msg, pointer_at).unwrap(), ("lamp._mrroip._tcp.local".to_string(), msg.len()));
    }
}
