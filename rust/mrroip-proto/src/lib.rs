// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! MRRoIP protocol types shared by the client, the GUI and the endpoint core (MRROIP-1.md).
//!
//! Nothing here touches a socket. The wire documents are plain serde types that keep every field they do not
//! know in `extra`, so a newer endpoint never loses information on its way through an older master.

pub mod decl;
pub mod protocol;
pub mod ssdp;
pub mod types;

pub use decl::{Decl, ValueType};
pub use types::*;
