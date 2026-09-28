// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! Protocol constants (MRROIP-1.md §2.4, §5.3, §9.3). The name is spelled here and nowhere else in the Rust
//! crates, as `protocol.py` and `proto_name.h` do for Python and C.

pub const NAME: &str = "MRRoIP";
pub const TOKEN: &str = "mrroip";
pub const VERSION: &str = "0.1";

pub const HTTP_PORT: u16 = 80;
pub const UDP_PORT: u16 = 5300;
pub const WHOIS_PORT: u16 = 8266;
pub const SSDP_ADDR: [u8; 4] = [239, 255, 255, 250];
pub const SSDP_PORT: u16 = 1900;
pub const SSDP_ST: &str = "urn:schemas-mrroip-org:device:Endpoint:1";
/// X-MRROIP-ID, -NAME, -TYPE, -CLASS (§6.1); -Seq and -Base on uploads (§9.8)
pub const HEADER_PREFIX: &str = "X-MRROIP-";
/// + "._tcp" (§6.2)
pub const MDNS_SERVICE: &str = "_mrroip";
pub const MDNS_ADDR: [u8; 4] = [224, 0, 0, 251];
pub const MDNS_PORT: u16 = 5353;

/// §9.3, in the order `capabilities.core_modes` lists them
pub const CORE_MODES: [&str; 4] = ["estop", "reset", "release", "hold"];
/// §2.3: the limit on every reserved path
pub const BODY_MAX: usize = 4096;
/// §9.5
pub const REPLAY_WINDOW_MS: u64 = 5000;

/// The `mrroip` part of the USN, `uuid:mrroip-a0b765123456::<ST>` (§6.1)
pub fn usn(device_id: &str) -> String {
    format!("uuid:{TOKEN}-{}::{SSDP_ST}", device_id.replace(':', ""))
}

/// The device id in a USN, if it is an MRRoIP one: what a byebye carries instead of `X-MRROIP-ID`
pub fn device_id_from_usn(usn: &str) -> Option<String> {
    let rest = usn.strip_prefix("uuid:")?.strip_prefix(TOKEN)?.strip_prefix('-')?;
    let hex: String = rest.chars().take_while(|c| c.is_ascii_hexdigit()).collect();
    if hex.len() != 12 {
        return None;
    }
    let hex = hex.to_ascii_lowercase();
    Some((0..6).map(|i| &hex[2 * i..2 * i + 2]).collect::<Vec<_>>().join(":"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn usn_round_trip() {
        let u = usn("a0:b7:65:12:34:56");
        assert_eq!(u, "uuid:mrroip-a0b765123456::urn:schemas-mrroip-org:device:Endpoint:1");
        assert_eq!(device_id_from_usn(&u).as_deref(), Some("a0:b7:65:12:34:56"));
        assert_eq!(device_id_from_usn("uuid:other-a0b765123456"), None);
    }
}
