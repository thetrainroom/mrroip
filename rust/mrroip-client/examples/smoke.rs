// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! Exercises the client against a live endpoint with the `reference` profile: definition and its ETag,
//! a refused and an accepted config write, control over HTTP and UDP, and holding authority.
//!
//!     cargo run -p mrroip-client --example smoke -- 192.168.10.155:8080

use std::sync::mpsc;
use std::time::Duration;

use mrroip_client::proto::ControlMessage;
use mrroip_client::{ConfigOutcome, Device, Keeper, KeeperEvent};
use serde_json::{Map, json};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let addr = std::env::args().nth(1).ok_or("usage: smoke <ip[:port]>")?;
    let dev = Device::new(&addr)?;
    let d = dev.definition()?;
    println!("definition: {} {} modes {:?}", d.device_name, d.device_type, d.capabilities.modes);
    assert_eq!(dev.definition()?, d, "revalidated definition differs");

    let mut bad = Map::new();
    bad.insert("blink_period_ms".into(), json!(1));
    match dev.set_config(&bad, false, None)? {
        ConfigOutcome::Refused(e) => println!("refused as expected: {} {:?}", e.error, e.details[0].reason),
        other => return Err(format!("expected a refusal, got {other:?}").into()),
    }
    let version = dev.config()?.meta.config_version;
    let mut name = Map::new();
    name.insert("device_name".into(), json!(format!("smoke-{version}")));
    let ConfigOutcome::Applied(c) = dev.set_config(&name, false, Some(version))? else { return Err("rename refused".into()) };
    println!("renamed: {} dirty {:?}", c.config["device_name"], c.meta.dirty_keys);
    assert_eq!(dev.definition()?.device_name, format!("smoke-{version}"), "ETag served a stale definition");
    let ConfigOutcome::Conflict(_) = dev.set_config(&name, false, Some(version))? else { return Err("stale if_version accepted".into()) };
    println!("stale if_version: conflict as expected");

    let (status, r) = dev.control(ControlMessage::mode("on").with_object("level", json!(40)))?;
    println!("http control: {status} accepted={} lamp={}", r.accepted, r.state.as_ref().unwrap().profile["lamp"]);
    let r = dev.control_udp(ControlMessage::mode("blink"))?;
    println!("udp control: accepted={} busy={}", r.accepted, r.state.as_ref().unwrap().busy);
    let (_, r) = dev.control(ControlMessage::mode("on").with_object("level", json!(101)))?;
    println!("out of range: {:?} {:?}", r.error, r.details.first().map(|d| &d.reason));

    let (tx, rx) = mpsc::channel();
    let keeper = Keeper::start(dev.clone(), ControlMessage::mode("on"), Duration::from_millis(300), tx)?;
    std::thread::sleep(Duration::from_secs(3));
    let replies = rx.try_iter().filter(|e| matches!(e, KeeperEvent::Reply(r) if r.accepted)).count();
    println!("keeper: {replies} accepted repeats; authority {}", dev.state()?.authority);
    keeper.release();
    std::thread::sleep(Duration::from_millis(200));
    println!("after release: authority {}", dev.state()?.authority);
    Ok(())
}
