// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! Lists every endpoint found by SSDP search, whois broadcast and mDNS, like `discovery_check.py --scan`.
//!
//!     cargo run -p mrroip-client --example find [local-ip]

use std::time::Duration;

use mrroip_client::discovery::{self, Found};

fn main() -> std::io::Result<()> {
    let iface = std::env::args().nth(1).and_then(|a| a.parse().ok());
    let show = |f: Found| {
        println!(
            "{:<7} {:<15} :{:<5} {:<17} {:<20} {}/{}",
            format!("{:?}", f.via),
            f.ip,
            f.http_port,
            f.device_id.as_deref().unwrap_or("?"),
            f.name.as_deref().unwrap_or("?"),
            f.device_type.as_deref().unwrap_or("?"),
            f.device_class.as_deref().unwrap_or("?"),
        )
    };
    let (mut a, mut b, mut c) = (show, show, show);
    discovery::ssdp_search(Duration::from_secs(3), iface, &mut a)?;
    discovery::whois(None, Duration::from_secs(2), iface, &mut b)?;
    discovery::mdns_browse(Duration::from_secs(2), iface, &mut c)?;
    Ok(())
}
