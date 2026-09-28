// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! Prints what an endpoint declares, and checks values against a declaration as the desktop app does.
//!
//!     cargo run -p mrroip-client --example show -- 192.168.10.165
//!     cargo run -p mrroip-client --example show -- 192.168.10.165 stream '"off"'

use mrroip_client::Device;
use mrroip_client::proto::Decl;

fn describe(d: &Decl) -> String {
    match &d.one_of {
        Some(forms) => format!("one of [{}]", forms.iter().map(describe).collect::<Vec<_>>().join(" | ")),
        None if d.type_.is_empty() => "(no type)".into(),
        None => d.type_.clone(),
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let addr = args.first().ok_or("usage: show <ip[:port]> [object value-as-JSON]")?;
    let d = Device::new(addr)?.definition()?;
    println!("{} {} ({}), firmware {}", d.device_name, d.device_type, d.device_class, d.firmware);
    for o in &d.objects {
        println!("  object {:<12} {}", o.key(), describe(o));
    }
    if let (Some(id), Some(value)) = (args.get(1), args.get(2)) {
        let object = d.object(id).ok_or("no such object")?;
        let value: serde_json::Value = serde_json::from_str(value)?;
        println!("{id} = {value}: {}", object.check(&value).err().unwrap_or("accepted"));
    }
    Ok(())
}
