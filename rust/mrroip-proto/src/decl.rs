// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! The declaration grammar shared by parameters and objects (MRROIP-1.md §7.2, §7.3), and validation of a
//! value against it. The same code renders a widget in the GUI and refuses a write in the endpoint core, so
//! the two cannot disagree about what a declaration permits.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Number, Value};

/// One entry of `parameters[]` or `objects[]`, or one field of an `object[]` record.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Decl {
    /// Objects are keyed by `id`
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    /// Parameters and record fields by `name`
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    /// Empty when a declaration names no type: it still loads, and is shown and edited as JSON
    #[serde(rename = "type", default)]
    pub type_: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub default: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min: Option<Number>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max: Option<Number>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_len: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub values: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub count: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub min_count: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_count: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fields: Option<Vec<Decl>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub unit: Option<String>,
    /// `control` (the default) or `state` (§7.3)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub access: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub confirms: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub persist: Option<bool>,
    /// `restart` for a parameter read once at start-up (§7.2)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub applies: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub doc: Option<String>,
    /// Everything else, e.g. an object's opaque `profile` block
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

/// The element type of a declaration, without the `[]`
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ValueType {
    Int,
    Float,
    String,
    Bool,
    Enum,
    Resource,
    /// `object[]`: a record with `fields`
    Record,
    /// A type this version does not know; accepted as is and rendered as JSON
    Other(String),
}

impl Decl {
    /// `id` for an object, `name` for a parameter: the key a message carries and the `ui` section uses
    pub fn key(&self) -> &str {
        self.id.as_deref().or(self.name.as_deref()).unwrap_or("")
    }

    /// The element type, and whether the value is a list of them
    pub fn value_type(&self) -> (ValueType, bool) {
        let (base, list) = match self.type_.strip_suffix("[]") {
            Some(base) => (base, true),
            None => (self.type_.as_str(), false),
        };
        let t = match base {
            "int" => ValueType::Int,
            "float" => ValueType::Float,
            "string" => ValueType::String,
            "bool" => ValueType::Bool,
            "enum" => ValueType::Enum,
            "resource" => ValueType::Resource,
            "object" => ValueType::Record,
            other => ValueType::Other(other.to_string()),
        };
        (t, list)
    }

    pub fn is_state_only(&self) -> bool {
        self.access.as_deref() == Some("state")
    }

    pub fn applies_at_restart(&self) -> bool {
        self.applies.as_deref() == Some("restart")
    }

    pub fn min_f64(&self) -> Option<f64> {
        self.min.as_ref().and_then(Number::as_f64)
    }

    pub fn max_f64(&self) -> Option<f64> {
        self.max.as_ref().and_then(Number::as_f64)
    }

    /// Checks a whole value against the declaration. Returns the §8.2 reason on refusal:
    /// `wrong_type`, `out_of_range`, `too_long`, `not_allowed` or `wrong_count`.
    pub fn check(&self, value: &Value) -> Result<(), &'static str> {
        let (t, list) = self.value_type();
        if !list {
            return self.check_element(&t, value);
        }
        let items = value.as_array().ok_or("wrong_type")?;
        let n = items.len() as u32;
        let count_ok = match self.count {
            Some(count) => n == count,
            None => self.min_count.is_none_or(|m| n >= m) && self.max_count.is_none_or(|m| n <= m),
        };
        if !count_ok {
            return Err("wrong_count");
        }
        items.iter().try_for_each(|item| self.check_element(&t, item))
    }

    fn check_element(&self, t: &ValueType, value: &Value) -> Result<(), &'static str> {
        match t {
            ValueType::Int => {
                let n = value.as_number().ok_or("wrong_type")?;
                let f = n.as_f64().ok_or("wrong_type")?;
                if f.fract() != 0.0 {
                    return Err("wrong_type");
                }
                self.check_range(f)
            }
            ValueType::Float => self.check_range(value.as_f64().ok_or("wrong_type")?),
            ValueType::Bool => value.as_bool().map(|_| ()).ok_or("wrong_type"),
            ValueType::String | ValueType::Resource => {
                let s = value.as_str().ok_or("wrong_type")?;
                if self.max_len.is_some_and(|m| s.len() > m as usize) {
                    return Err("too_long");
                }
                self.check_values(s)
            }
            ValueType::Enum => self.check_values(value.as_str().ok_or("wrong_type")?),
            ValueType::Record => {
                let record = value.as_object().ok_or("wrong_type")?;
                let fields = self.fields.as_deref().unwrap_or(&[]);
                for (key, v) in record {
                    let field = fields.iter().find(|f| f.key() == key).ok_or("wrong_type")?;
                    field.check(v)?;
                }
                Ok(())
            }
            ValueType::Other(_) => Ok(()),
        }
    }

    fn check_range(&self, f: f64) -> Result<(), &'static str> {
        let low = self.min_f64().is_some_and(|m| f < m);
        let high = self.max_f64().is_some_and(|m| f > m);
        if low || high { Err("out_of_range") } else { Ok(()) }
    }

    fn check_values(&self, s: &str) -> Result<(), &'static str> {
        match &self.values {
            Some(values) if !values.iter().any(|v| v == s) => Err("not_allowed"),
            _ => Ok(()),
        }
    }

    /// The `details[]` entry for a refusal of this declaration's key (§8.2)
    pub fn detail(&self, reason: &str) -> crate::Detail {
        let mut d = crate::Detail::new(self.key(), reason);
        match reason {
            "out_of_range" => {
                d.min = self.min.clone();
                d.max = self.max.clone();
            }
            "too_long" => d.max_len = self.max_len,
            "not_allowed" => d.values = self.values.clone(),
            _ => {}
        }
        d
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn decl(v: Value) -> Decl {
        serde_json::from_value(v).unwrap()
    }

    #[test]
    fn int_range_and_type() {
        let d = decl(json!({"name": "p", "type": "int", "min": 50, "max": 5000, "default": 500}));
        assert_eq!(d.check(&json!(50)), Ok(()));
        assert_eq!(d.check(&json!(5000.0)), Ok(()));
        assert_eq!(d.check(&json!(49)), Err("out_of_range"));
        assert_eq!(d.check(&json!(1.5)), Err("wrong_type"));
        assert_eq!(d.check(&json!("fast")), Err("wrong_type"));
        assert_eq!(d.check(&json!(true)), Err("wrong_type"));
        let detail = serde_json::to_value(d.detail("out_of_range")).unwrap();
        assert_eq!(detail, json!({"key": "p", "reason": "out_of_range", "min": 50, "max": 5000}));
    }

    #[test]
    fn strings_enums_lists_records() {
        let s = decl(json!({"name": "n", "type": "string", "max_len": 3}));
        assert_eq!(s.check(&json!("abc")), Ok(()));
        assert_eq!(s.check(&json!("abcd")), Err("too_long"));
        let e = decl(json!({"id": "lamp", "type": "enum", "values": ["off", "on"]}));
        assert_eq!(e.key(), "lamp");
        assert_eq!(e.check(&json!("dim")), Err("not_allowed"));
        let l = decl(json!({"name": "d", "type": "int[]", "count": 2, "min": 0, "max": 9}));
        assert_eq!(l.check(&json!([1, 2])), Ok(()));
        assert_eq!(l.check(&json!([1])), Err("wrong_count"));
        assert_eq!(l.check(&json!([1, 10])), Err("out_of_range"));
        let r = decl(json!({"name": "zones", "type": "object[]", "max_count": 2,
                            "fields": [{"name": "label", "type": "string", "max_len": 4}]}));
        assert_eq!(r.value_type(), (ValueType::Record, true));
        assert_eq!(r.check(&json!([{"label": "a"}])), Ok(()));
        assert_eq!(r.check(&json!([{"label": "abcde"}])), Err("too_long"));
        assert_eq!(r.check(&json!([{"zz": 1}])), Err("wrong_type"));
    }

    #[test]
    fn a_declaration_without_a_type_still_loads() {
        // what display firmware 0.1.0 sends: a pre-grammar `kind`, no `type`
        let d = decl(json!({"id": "screen", "kind": "output", "states": ["on", "off"]}));
        assert_eq!(d.value_type(), (ValueType::Other(String::new()), false));
        assert_eq!(d.check(&json!("on")), Ok(()));
        assert_eq!(d.extra["kind"], "output");
    }

    #[test]
    fn unknown_fields_survive() {
        let v = json!({"id": "image", "type": "resource", "profile": {"width_px": 64}});
        let d = decl(v.clone());
        assert_eq!(serde_json::to_value(&d).unwrap(), v);
    }
}
