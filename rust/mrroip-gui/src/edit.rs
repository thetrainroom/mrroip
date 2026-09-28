// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! A widget for any value, chosen from its declaration (MRROIP-1.md §7.2): the same grammar for parameters
//! and objects, so one function draws both. Types without a natural widget — lists, records, types this
//! version does not know — are edited as JSON text and checked against the declaration before sending.

use eframe::egui::{self, Color32, ComboBox, DragValue, Slider, TextEdit, Ui};
use mrroip_proto::{Decl, Definition, ValueType};
use serde_json::Value;

/// A value being edited
#[derive(Clone, Debug)]
pub struct Field {
    pub value: Value,
    /// The text of a string or JSON field
    pub text: String,
    /// The text does not parse as JSON
    pub invalid: bool,
    pub changed: bool,
}

impl Field {
    pub fn new(value: Value) -> Field {
        let text = match &value {
            Value::String(s) => s.clone(),
            other => other.to_string(),
        };
        Field { value, text, invalid: false, changed: false }
    }

    /// The value, if the declaration accepts it; otherwise the §8.2 reason
    pub fn checked(&self, decl: &Decl) -> Result<&Value, &'static str> {
        if self.invalid {
            return Err("wrong_type");
        }
        decl.check(&self.value).map(|_| &self.value)
    }
}

/// Draws the widget for `decl` and edits `field`. Returns true when the user changed it.
pub fn field_ui(ui: &mut Ui, id: &str, decl: &Decl, def: &Definition, field: &mut Field, enabled: bool) -> bool {
    let (t, list) = decl.value_type();
    let unit = decl.unit.as_deref().map(|u| format!(" {u}")).unwrap_or_default();
    let before = field.value.clone();
    ui.add_enabled_ui(enabled, |ui| match (t, list) {
        (ValueType::Int, false) => {
            let mut n = field.value.as_f64().unwrap_or(0.0).round() as i64;
            match (decl.min_f64(), decl.max_f64()) {
                (Some(lo), Some(hi)) if hi - lo <= 10_000.0 => {
                    ui.add(Slider::new(&mut n, lo as i64..=hi as i64).suffix(unit));
                }
                (lo, hi) => {
                    let range = lo.map_or(i64::MIN, |v| v as i64)..=hi.map_or(i64::MAX, |v| v as i64);
                    ui.add(DragValue::new(&mut n).range(range).suffix(unit));
                }
            }
            field.value = n.into();
        }
        (ValueType::Float, false) => {
            let mut f = field.value.as_f64().unwrap_or(0.0);
            match (decl.min_f64(), decl.max_f64()) {
                (Some(lo), Some(hi)) => ui.add(Slider::new(&mut f, lo..=hi).suffix(unit)),
                _ => ui.add(DragValue::new(&mut f).speed(0.01).suffix(unit)),
            };
            field.value = serde_json::Number::from_f64(f).map(Value::Number).unwrap_or(Value::Null);
        }
        (ValueType::Bool, false) => {
            let mut b = field.value.as_bool().unwrap_or(false);
            ui.checkbox(&mut b, "");
            field.value = b.into();
        }
        (ValueType::Enum | ValueType::String, false) if decl.values.is_some() => {
            let current = field.value.as_str().unwrap_or("").to_string();
            let labels = def.ui.get(decl.key()).and_then(|u| u.value_labels.clone()).unwrap_or_default();
            let label = |v: &str| labels.get(v).cloned().unwrap_or_else(|| v.to_string());
            let mut selected = current.clone();
            ComboBox::from_id_salt(id).selected_text(label(&current)).show_ui(ui, |ui| {
                for v in decl.values.iter().flatten() {
                    ui.selectable_value(&mut selected, v.clone(), label(v));
                }
            });
            field.value = selected.into();
        }
        (ValueType::String | ValueType::Resource, false) => {
            let mut edit = TextEdit::singleline(&mut field.text).desired_width(220.0);
            if let Some(max) = decl.max_len {
                edit = edit.char_limit(max as usize);
            }
            ui.add(edit);
            field.value = Value::String(field.text.clone());
        }
        _ => {
            // lists, records and unknown types: JSON text
            let response = ui.add(TextEdit::multiline(&mut field.text).code_editor().desired_rows(1).desired_width(320.0));
            if response.changed() {
                match serde_json::from_str(&field.text) {
                    Ok(v) => {
                        field.value = v;
                        field.invalid = false;
                    }
                    Err(_) => field.invalid = true,
                }
            }
            if field.invalid {
                ui.colored_label(Color32::RED, "not JSON");
            }
        }
    });
    if field.value != before {
        if let Value::String(s) = &field.value {
            field.text = s.clone();
        } else if !matches!(decl.value_type(), (ValueType::Other(_) | ValueType::Record, _) | (_, true)) {
            field.text = field.value.to_string();
        }
        field.changed = true;
        return true;
    }
    false
}

/// A value for display: strings bare, everything else as compact JSON
pub fn show_value(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => "—".into(),
        other => other.to_string(),
    }
}

/// Hover text for an entry: its `ui` doc, else its declaration doc
pub fn doc<'a>(def: &'a Definition, decl: &'a Decl) -> Option<&'a str> {
    def.ui.get(decl.key()).and_then(|u| u.doc.as_deref()).or(decl.doc.as_deref())
}

pub fn heading(ui: &mut Ui, text: &str) {
    ui.add_space(6.0);
    ui.label(egui::RichText::new(text).strong());
}
