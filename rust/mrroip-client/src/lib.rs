// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! Find, configure and control MRRoIP endpoints: the Rust counterpart of the `mrroip` Python package.
//!
//! ```no_run
//! use mrroip_client::{Device, discovery};
//! use std::time::Duration;
//!
//! discovery::ssdp_search(Duration::from_secs(3), None, &mut |f| println!("{} {:?}", f.ip, f.name)).unwrap();
//! let dev = Device::new("192.168.10.164").unwrap();
//! println!("{:?}", dev.definition().unwrap().capabilities.modes);
//! ```

pub mod device;
pub mod discovery;
pub mod http;
pub mod keeper;

pub use device::{ConfigOutcome, Device, Error, Result};
pub use keeper::{Keeper, KeeperEvent};
pub use mrroip_proto as proto;
