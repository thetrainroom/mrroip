// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! The wire documents: `/definition` (§7), `/config` (§8), control messages and responses (§9), `/state`
//! (§9.6) and the whois reply (§6.3). Unknown fields are kept in `extra`.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value};

use crate::Decl;

fn is_false(b: &bool) -> bool {
    !*b
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Definition {
    pub proto: String,
    pub proto_version: String,
    pub device_id: String,
    pub device_name: String,
    pub device_type: String,
    pub device_class: String,
    pub profile_version: String,
    pub firmware: String,
    /// An open map (§7.5): a path, `udp_control_port`, or `{path, methods, content_type, max_bytes, doc}`
    #[serde(default)]
    pub endpoints: Map<String, Value>,
    #[serde(default)]
    pub capabilities: Capabilities,
    #[serde(default)]
    pub objects: Vec<Decl>,
    #[serde(default)]
    pub parameters: Vec<Decl>,
    /// Presentation, keyed by object id or parameter name (§7.7)
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub ui: BTreeMap<String, UiEntry>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

impl Definition {
    /// The path of an `endpoints` entry, whether it is written as a string or as `{path, …}`
    pub fn endpoint_path(&self, name: &str) -> Option<&str> {
        match self.endpoints.get(name)? {
            Value::String(s) => Some(s),
            Value::Object(o) => o.get("path")?.as_str(),
            _ => None,
        }
    }

    pub fn udp_control_port(&self) -> Option<u16> {
        self.endpoints.get("udp_control_port")?.as_u64().and_then(|p| u16::try_from(p).ok())
    }

    pub fn parameter(&self, name: &str) -> Option<&Decl> {
        self.parameters.iter().find(|p| p.key() == name)
    }

    pub fn object(&self, id: &str) -> Option<&Decl> {
        self.objects.iter().find(|o| o.key() == id)
    }

    /// The label a user sees: the `ui` label, or the name itself when there is none (§7.7)
    pub fn label<'a>(&'a self, name: &'a str) -> &'a str {
        self.ui.get(name).and_then(|u| u.label.as_deref()).unwrap_or(name)
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Capabilities {
    #[serde(default)]
    pub autonomous: bool,
    #[serde(default)]
    pub commanded: bool,
    #[serde(default, skip_serializing_if = "is_false")]
    pub reporting: bool,
    #[serde(default)]
    pub telemetry_hz: f64,
    #[serde(default)]
    pub modes: Vec<String>,
    #[serde(default)]
    pub core_modes: Vec<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub languages: Vec<String>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct UiEntry {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub value_labels: Option<BTreeMap<String, String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub doc: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub icon: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub group: Option<String>,
    #[serde(default, skip_serializing_if = "is_false")]
    pub advanced: bool,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// `GET /config`, and the body of an accepted `POST /config` (§8.1)
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ConfigReply {
    pub device_id: String,
    pub config: Map<String, Value>,
    #[serde(rename = "_meta", default)]
    pub meta: Meta,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Meta {
    pub config_version: u64,
    #[serde(default)]
    pub dirty: bool,
    #[serde(default)]
    pub dirty_keys: Vec<String>,
    #[serde(default)]
    pub restart_pending_keys: Vec<String>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// One entry of `details[]`: a refused config key or object (§8.2, §9.4)
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Detail {
    pub key: String,
    pub reason: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min: Option<Number>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max: Option<Number>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_len: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub values: Option<Vec<String>>,
}

impl Detail {
    pub fn new(key: &str, reason: &str) -> Self {
        Detail { key: key.into(), reason: reason.into(), min: None, max: None, max_len: None, values: None }
    }
}

/// A refused `POST /config`, or any other error body
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ErrorBody {
    pub error: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub applied: Option<bool>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub details: Vec<Detail>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// A control message (§9.2). The same body goes over HTTP and UDP.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ControlMessage {
    pub seq: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ts: Option<u64>,
    pub mode: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<Value>,
    #[serde(default, skip_serializing_if = "Map::is_empty")]
    pub objects: Map<String, Value>,
    #[serde(default, skip_serializing_if = "is_false")]
    pub hold: bool,
}

impl ControlMessage {
    /// A message with only a mode; the sender fills in `seq`
    pub fn mode(mode: &str) -> Self {
        ControlMessage { mode: mode.into(), ..Default::default() }
    }

    pub fn with_object(mut self, id: &str, value: Value) -> Self {
        self.objects.insert(id.into(), value);
        self
    }
}

/// The response to a control message or an upload (§9.4)
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ControlResponse {
    #[serde(default)]
    pub device_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ack_seq: Option<Number>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ts: Option<Number>,
    pub accepted: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// With `missing_field`: which one
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub field: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub details: Vec<Detail>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub authority_taken_from: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub state: Option<State>,
}

/// `state` of a control response, and `GET /state` (§9.4, §9.6)
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct State {
    #[serde(default)]
    pub mode: String,
    #[serde(default)]
    pub authority: String,
    #[serde(default)]
    pub busy: bool,
    #[serde(default)]
    pub fault: Option<String>,
    #[serde(default)]
    pub uptime_ms: u64,
    #[serde(default)]
    pub profile: Map<String, Value>,
    /// `/state` only (§9.6)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub free_heap: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub network: Option<String>,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// The whois reply (§6.3)
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct WhoisReply {
    pub proto: String,
    pub v: String,
    pub id: String,
    pub name: String,
    #[serde(rename = "type")]
    pub type_: String,
    pub class: String,
    pub ip: String,
    pub definition: String,
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn definition_from_the_c_core() {
        let v = json!({
            "proto": "MRRoIP", "proto_version": "0.1", "device_id": "a0:b7:65:12:34:56", "device_name": "x",
            "device_type": "display", "device_class": "passive", "profile_version": "1.0", "firmware": "0.2.0",
            "endpoints": {"definition": "/definition", "config": "/config", "control": "/control",
                          "state": "/state", "objects": "/objects/{id}", "udp_control_port": 5300,
                          "image_upload": {"path": "/objects/image", "methods": ["PUT"]}},
            "capabilities": {"autonomous": false, "commanded": true, "telemetry_hz": 0, "modes": ["show", "blink"],
                             "core_modes": ["estop", "reset", "release", "hold"]},
            "objects": [{"id": "image", "type": "resource", "profile": {"width_px": 128}}],
            "parameters": [{"name": "device_name", "type": "string", "default": "x", "max_len": 31,
                            "persist": true}],
            "ui": {"device_name": {"label": "Name", "group": "General"}}
        });
        let d: Definition = serde_json::from_value(v).unwrap();
        assert_eq!(d.udp_control_port(), Some(5300));
        assert_eq!(d.endpoint_path("image_upload"), Some("/objects/image"));
        assert_eq!(d.capabilities.modes, ["show", "blink"]);
        assert_eq!(d.label("device_name"), "Name");
        assert_eq!(d.label("image"), "image");
        assert_eq!(d.object("image").unwrap().extra["profile"]["width_px"], 128);
    }

    #[test]
    fn control_round_trip() {
        let m = ControlMessage { seq: 7, ..ControlMessage::mode("on") }.with_object("level", json!(40));
        assert_eq!(serde_json::to_value(&m).unwrap(), json!({"seq": 7, "mode": "on", "objects": {"level": 40}}));
        let r: ControlResponse = serde_json::from_value(json!({
            "device_id": "x", "ack_seq": 7, "accepted": false, "error": "invalid_object_state",
            "details": [{"key": "level", "reason": "out_of_range"}],
            "state": {"mode": "on", "authority": "commanded", "busy": false, "fault": null, "uptime_ms": 5,
                      "profile": {"level": 30}}})).unwrap();
        assert_eq!(r.details[0].reason, "out_of_range");
        assert_eq!(r.state.unwrap().profile["level"], 30);
    }
}
