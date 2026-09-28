// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! One endpoint, open: what it is, its configuration, its control and its state. Rendered entirely from its
//! `/definition` (MRROIP-1.md §7): labels, groups and `advanced` from the `ui` section, widgets from the
//! declarations, names only where no label exists (§7.7).

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use eframe::egui::{self, Color32, RichText, Ui};
use mrroip_client::{ConfigOutcome, Device, Keeper, KeeperEvent};
use mrroip_proto::protocol::CORE_MODES;
use mrroip_proto::{ConfigReply, ControlMessage, ControlResponse, Decl, Definition, Detail, State};
use serde_json::{Map, Value};

use crate::app::{Msg, StopFlag, Tx};
use crate::edit::{self, Field};

#[derive(Clone, Copy, PartialEq, Eq)]
enum Tab {
    Overview,
    Config,
    Control,
    State,
}

const RED: Color32 = Color32::from_rgb(200, 40, 40);
const AMBER: Color32 = Color32::from_rgb(200, 120, 0);
const GREEN: Color32 = Color32::from_rgb(40, 150, 70);

pub struct Session {
    pub key: String,
    device: Device,
    tx: Tx,
    tab: Tab,
    definition: Option<Definition>,
    config: Option<ConfigReply>,
    load_error: Option<String>,
    state: Option<State>,
    state_error: Option<String>,
    _poller: StopFlag,

    fields: BTreeMap<String, Field>,
    persist: bool,
    refused: HashMap<String, String>,
    config_note: Option<(Color32, String)>,
    config_busy: bool,

    mode: String,
    objects: BTreeMap<String, (bool, Field)>,
    udp: bool,
    keeper: Option<Keeper>,
    reply: Option<ControlResponse>,
    control_note: Option<(Color32, String)>,
}

impl Session {
    pub fn open(key: String, device: Device, tx: Tx) -> Session {
        let flag = Arc::new(AtomicBool::new(false));
        let session = Session {
            key,
            device,
            tx,
            tab: Tab::Overview,
            definition: None,
            config: None,
            load_error: None,
            state: None,
            state_error: None,
            _poller: StopFlag(flag.clone()),
            fields: BTreeMap::new(),
            persist: true,
            refused: HashMap::new(),
            config_note: None,
            config_busy: false,
            mode: String::new(),
            objects: BTreeMap::new(),
            udp: false,
            keeper: None,
            reply: None,
            control_note: None,
        };
        session.load();
        session.poll(flag);
        session
    }

    fn load(&self) {
        let (key, device) = (self.key.clone(), self.device.clone());
        self.tx.spawn(move || {
            let result = device.definition().and_then(|d| Ok((d, device.config()?))).map_err(|e| e.to_string());
            Msg::Loaded(key, result)
        });
    }

    /// `/state` at 2 Hz while the device is open. Polling never holds authority (§9.6).
    fn poll(&self, stop: Arc<AtomicBool>) {
        let (key, device, tx) = (self.key.clone(), self.device.clone(), self.tx.clone());
        thread::spawn(move || {
            let mut device = device;
            device.timeout = Duration::from_secs(2);
            while !stop.load(Ordering::Relaxed) {
                tx.send(Msg::State(key.clone(), device.state().map_err(|e| e.to_string())));
                thread::sleep(Duration::from_millis(500));
            }
        });
    }

    pub fn receive(&mut self, msg: Msg) {
        match msg {
            Msg::Loaded(key, result) if key == self.key => match result {
                Ok((d, c)) => {
                    self.set_config(c);
                    if self.mode.is_empty() || !d.capabilities.modes.contains(&self.mode) {
                        self.mode = d.capabilities.modes.first().cloned().unwrap_or_default();
                    }
                    self.objects = d
                        .objects
                        .iter()
                        .filter(|o| !o.is_state_only())
                        .map(|o| (o.key().to_string(), (false, Field::new(o.default.clone().unwrap_or(Value::Null)))))
                        .collect();
                    self.definition = Some(d);
                    self.load_error = None;
                }
                Err(e) => self.load_error = Some(e),
            },
            Msg::Config(key, result) if key == self.key => {
                self.config_busy = false;
                match result {
                    Ok(ConfigOutcome::Applied(c)) => {
                        let restart = !c.meta.restart_pending_keys.is_empty() && self.persist;
                        self.set_config(c);
                        self.config_note = Some((GREEN, if restart { "Applied; the endpoint restarts".into() } else { "Applied".into() }));
                        self.load(); // a rename changes /definition
                    }
                    Ok(ConfigOutcome::Refused(e)) => {
                        self.refused = e.details.iter().map(|d| (d.key.clone(), describe(d))).collect();
                        self.config_note = Some((RED, format!("Nothing applied: {}", e.error)));
                    }
                    Ok(ConfigOutcome::Conflict(c)) => {
                        self.set_config(c);
                        self.config_note = Some((AMBER, "Changed elsewhere meanwhile; reloaded — check and apply again".into()));
                    }
                    Err(e) => self.config_note = Some((RED, e)),
                }
            }
            Msg::State(key, result) if key == self.key => match result {
                Ok(s) => {
                    self.state = Some(s);
                    self.state_error = None;
                }
                Err(e) => self.state_error = Some(e),
            },
            Msg::Control(key, result) if key == self.key => match result {
                Ok((_, reply)) => self.show_reply(reply),
                Err(e) => self.control_note = Some((RED, e)),
            },
            Msg::Keeper(key, event) if key == self.key => match event {
                KeeperEvent::Reply(reply) => {
                    if !reply.accepted || reply.authority_taken_from.is_some() {
                        self.show_reply(reply);
                    }
                }
                KeeperEvent::Silent(e) => self.control_note = Some((RED, format!("Holding authority: no answer ({e})"))),
            },
            _ => {}
        }
    }

    fn set_config(&mut self, c: ConfigReply) {
        self.fields = c.config.iter().map(|(k, v)| (k.clone(), Field::new(v.clone()))).collect();
        self.refused.clear();
        self.config = Some(c);
    }

    fn show_reply(&mut self, reply: ControlResponse) {
        self.control_note = Some(if reply.accepted {
            match &reply.authority_taken_from {
                Some(from) => (AMBER, format!("Accepted — authority taken from {from}")),
                None => (GREEN, "Accepted".into()),
            }
        } else {
            let details: Vec<String> = reply.details.iter().map(|d| format!("{}: {}", d.key, describe(d))).collect();
            let field = reply.field.as_deref().map(|f| format!(" ({f})")).unwrap_or_default();
            (RED, format!("Refused: {}{field} {}", reply.error.as_deref().unwrap_or("?"), details.join("; ")))
        });
        if let Some(s) = &reply.state {
            self.state = Some(s.clone());
        }
        self.reply = Some(reply);
    }

    fn send(&mut self, msg: ControlMessage) {
        if let Some(k) = &self.keeper
            && (!CORE_MODES.contains(&msg.mode.as_str()) || msg.mode == "hold") {
                k.desire(msg.clone());
            }
        let (key, device, udp) = (self.key.clone(), self.device.clone(), self.udp || msg.mode == "estop");
        self.tx.spawn(move || {
            let result = if udp { device.control_udp(msg).map(|r| (200, r)) } else { device.control(msg) };
            Msg::Control(key, result.map_err(|e| e.to_string()))
        });
    }

    fn desired(&self) -> ControlMessage {
        let objects: Map<String, Value> =
            self.objects.iter().filter(|(_, (on, _))| *on).map(|(k, (_, f))| (k.clone(), f.value.clone())).collect();
        ControlMessage { objects, ..ControlMessage::mode(&self.mode) }
    }

    fn hold_authority(&mut self, on: bool) {
        if !on {
            if let Some(k) = self.keeper.take() {
                k.release();
            }
            return;
        }
        let (events, inbox) = mpsc::channel();
        match Keeper::start(self.device.clone(), self.desired(), Duration::from_millis(500), events) {
            Ok(k) => {
                self.keeper = Some(k);
                let (key, tx) = (self.key.clone(), self.tx.clone());
                thread::spawn(move || {
                    while let Ok(event) = inbox.recv() {
                        tx.send(Msg::Keeper(key.clone(), event));
                    }
                });
            }
            Err(e) => self.control_note = Some((RED, e.to_string())),
        }
    }

    // ------------------------------------------------------------------------------------------------ drawing

    pub fn ui(&mut self, ui: &mut Ui) {
        let Some(def) = self.definition.clone() else {
            match &self.load_error {
                Some(e) => {
                    ui.colored_label(RED, format!("{}: {e}", self.device.host()));
                    if ui.button("Retry").clicked() {
                        self.load();
                    }
                }
                None => {
                    ui.spinner();
                }
            }
            return;
        };

        ui.horizontal(|ui| {
            ui.heading(&def.device_name);
            ui.label(RichText::new(format!("{} · {} · {}", def.device_type, def.device_class, self.device.host())).weak());
            ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                let estop = egui::Button::new(RichText::new("  STOP  ").strong().color(Color32::WHITE)).fill(RED);
                if ui.add(estop).on_hover_text("estop: halt everything now; latches until Reset (§9.3)").clicked() {
                    self.send(ControlMessage::mode("estop"));
                }
                self.status_line(ui);
            });
        });
        ui.horizontal(|ui| {
            for (tab, name) in [(Tab::Overview, "Overview"), (Tab::Config, "Configuration"), (Tab::Control, "Control"), (Tab::State, "State")] {
                ui.selectable_value(&mut self.tab, tab, name);
            }
        });
        ui.separator();
        egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| match self.tab {
            Tab::Overview => self.overview(ui, &def),
            Tab::Config => self.config_tab(ui, &def),
            Tab::Control => self.control_tab(ui, &def),
            Tab::State => self.state_tab(ui, &def),
        });
    }

    fn status_line(&self, ui: &mut Ui) {
        if let Some(e) = &self.state_error {
            ui.colored_label(RED, "not answering").on_hover_text(e);
            return;
        }
        if let Some(s) = &self.state {
            if let Some(f) = &s.fault {
                ui.colored_label(RED, format!("fault: {f}"));
            }
            if s.mode == "estop" {
                ui.colored_label(RED, "STOPPED");
            }
            ui.label(format!("{} · {}{}", s.mode, s.authority, if s.busy { " · busy" } else { "" }));
        }
    }

    fn overview(&self, ui: &mut Ui, def: &Definition) {
        egui::Grid::new("identity").num_columns(2).striped(true).show(ui, |ui| {
            let rows = [
                ("Name", def.device_name.clone()),
                ("Device id", def.device_id.clone()),
                ("Type", format!("{} (profile {})", def.device_type, def.profile_version)),
                ("Class", def.device_class.clone()),
                ("Firmware", def.firmware.clone()),
                ("Protocol", format!("{} {}", def.proto, def.proto_version)),
                ("Modes", def.capabilities.modes.join(", ")),
                ("Autonomous", def.capabilities.autonomous.to_string()),
            ];
            for (k, v) in rows {
                ui.label(k);
                ui.label(v);
                ui.end_row();
            }
        });
        edit::heading(ui, "Resources");
        egui::Grid::new("endpoints").num_columns(2).show(ui, |ui| {
            for (name, value) in &def.endpoints {
                ui.label(name);
                ui.label(edit::show_value(value));
                ui.end_row();
            }
        });
        if let Some(path) = def.endpoint_path("ui") {
            ui.add_space(6.0);
            ui.hyperlink_to("Open the endpoint's own interface", format!("http://{}{path}", self.device.host()));
        }
        let class_note = match def.device_class.as_str() {
            "mobile" => "Stops when its master goes quiet.",
            "stationary" => "Resumes its own programme, or comes to rest, when its master goes quiet.",
            "passive" => "Holds its last state when its master goes quiet.",
            _ => "",
        };
        ui.add_space(6.0);
        ui.label(RichText::new(class_note).weak());
    }

    /// Declarations in `ui` groups, in declaration order; `advanced` ones folded away
    fn grouped<'a>(def: &'a Definition, decls: &'a [Decl]) -> Vec<(String, Vec<&'a Decl>, Vec<&'a Decl>)> {
        let mut groups: Vec<(String, Vec<&Decl>, Vec<&Decl>)> = Vec::new();
        for d in decls {
            let ui = def.ui.get(d.key());
            let group = ui.and_then(|u| u.group.clone()).unwrap_or_default();
            let at = match groups.iter().position(|(g, _, _)| *g == group) {
                Some(i) => i,
                None => {
                    groups.push((group, Vec::new(), Vec::new()));
                    groups.len() - 1
                }
            };
            if ui.is_some_and(|u| u.advanced) { groups[at].2.push(d) } else { groups[at].1.push(d) }
        }
        groups
    }

    fn config_tab(&mut self, ui: &mut Ui, def: &Definition) {
        let Some(config) = self.config.clone() else { return };
        for (group, plain, advanced) in Self::grouped(def, &def.parameters) {
            edit::heading(ui, if group.is_empty() { "Settings" } else { &group });
            self.parameter_rows(ui, def, &config, &plain, &group);
            if !advanced.is_empty() {
                egui::CollapsingHeader::new("Advanced").id_salt(format!("adv-{group}")).show(ui, |ui| {
                    self.parameter_rows(ui, def, &config, &advanced, &format!("adv-{group}"));
                });
            }
        }
        ui.separator();
        let changed: Vec<String> = self.fields.iter().filter(|(_, f)| f.changed).map(|(k, _)| k.clone()).collect();
        ui.horizontal(|ui| {
            ui.checkbox(&mut self.persist, "Store").on_hover_text(
                "Stored values survive a restart. Unstored ones apply until the next restart and are shown as unsaved (§8.2).",
            );
            let apply = egui::Button::new(format!("Apply {} change{}", changed.len(), if changed.len() == 1 { "" } else { "s" }));
            if ui.add_enabled(!changed.is_empty() && !self.config_busy, apply).clicked() {
                self.apply(def, &config, &changed);
            }
            if ui.add_enabled(!self.config_busy, egui::Button::new("Reload")).clicked() {
                self.config_note = None;
                self.load();
            }
            if !config.meta.dirty_keys.is_empty() && ui.button("Store unsaved").on_hover_text("Write the running values to storage").clicked() {
                let values = config.meta.dirty_keys.iter().filter_map(|k| Some((k.clone(), config.config.get(k)?.clone()))).collect();
                self.post_config(values, true, Some(config.meta.config_version));
            }
            if let Some((colour, note)) = &self.config_note {
                ui.colored_label(*colour, note);
            }
        });
        ui.label(RichText::new(format!("config_version {}", config.meta.config_version)).weak());
    }

    fn parameter_rows(&mut self, ui: &mut Ui, def: &Definition, config: &ConfigReply, decls: &[&Decl], id: &str) {
        egui::Grid::new(format!("params-{id}")).num_columns(3).spacing([12.0, 6.0]).show(ui, |ui| {
            for decl in decls {
                let key = decl.key().to_string();
                let label = ui.label(def.label(&key));
                if let Some(doc) = edit::doc(def, decl) {
                    label.on_hover_text(doc);
                }
                let field = self.fields.entry(key.clone()).or_insert_with(|| Field::new(decl.default.clone().unwrap_or(Value::Null)));
                if edit::field_ui(ui, &format!("p-{key}"), decl, def, field, true) {
                    self.refused.remove(&key);
                }
                ui.horizontal(|ui| {
                    if let Some(reason) = self.refused.get(&key) {
                        ui.colored_label(RED, reason);
                    } else if let Err(reason) = field.checked(decl) {
                        ui.colored_label(RED, reason);
                    }
                    if config.meta.dirty_keys.contains(&key) {
                        ui.colored_label(AMBER, "unsaved").on_hover_text("Applied but not stored: lost at the next restart");
                    }
                    if config.meta.restart_pending_keys.contains(&key) {
                        ui.colored_label(AMBER, "restart pending");
                    } else if decl.applies_at_restart() {
                        ui.label(RichText::new("applies at restart").weak());
                    }
                });
                ui.end_row();
            }
        });
    }

    fn apply(&mut self, def: &Definition, config: &ConfigReply, changed: &[String]) {
        let mut values = Map::new();
        for key in changed {
            let (Some(field), Some(decl)) = (self.fields.get(key), def.parameter(key)) else { continue };
            match field.checked(decl) {
                Ok(v) => {
                    values.insert(key.clone(), v.clone());
                }
                Err(reason) => {
                    self.config_note = Some((RED, format!("{}: {reason}", def.label(key))));
                    return;
                }
            }
        }
        let restart = changed.iter().any(|k| def.parameter(k).is_some_and(Decl::applies_at_restart));
        if restart && !self.persist {
            self.config_note = Some((RED, "A value that applies at restart must be stored".into()));
            return;
        }
        self.post_config(values, self.persist, Some(config.meta.config_version));
    }

    fn post_config(&mut self, values: Map<String, Value>, persist: bool, if_version: Option<u64>) {
        self.config_busy = true;
        self.config_note = None;
        let (key, device) = (self.key.clone(), self.device.clone());
        self.tx.spawn(move || Msg::Config(key, device.set_config(&values, persist, if_version).map_err(|e| e.to_string())));
    }

    fn control_tab(&mut self, ui: &mut Ui, def: &Definition) {
        edit::heading(ui, "Desired state");
        ui.horizontal(|ui| {
            ui.label("Mode");
            egui::ComboBox::from_id_salt("mode").selected_text(def.label(&self.mode).to_string()).show_ui(ui, |ui| {
                for m in &def.capabilities.modes {
                    ui.selectable_value(&mut self.mode, m.clone(), def.label(m));
                }
            });
        });
        let controllable: Vec<&Decl> = def.objects.iter().filter(|o| !o.is_state_only()).collect();
        if !controllable.is_empty() {
            egui::Grid::new("objects").num_columns(3).spacing([12.0, 6.0]).show(ui, |ui| {
                for decl in controllable {
                    let key = decl.key().to_string();
                    let Some((on, field)) = self.objects.get_mut(&key) else { continue };
                    ui.checkbox(on, def.label(&key)).on_hover_text("Include this object in the message; objects left out keep their state (§9.2)");
                    if edit::field_ui(ui, &format!("o-{key}"), decl, def, field, true) {
                        *on = true;
                    }
                    if *on
                        && let Err(reason) = field.checked(decl) {
                            ui.colored_label(RED, reason);
                        }
                    ui.end_row();
                }
            });
        }
        ui.add_space(6.0);
        ui.horizontal(|ui| {
            if ui.add_enabled(!self.mode.is_empty(), egui::Button::new("Send")).clicked() {
                let msg = self.desired();
                self.send(msg);
            }
            ui.checkbox(&mut self.udp, "over UDP");
            let mut holding = self.keeper.is_some();
            if ui.checkbox(&mut holding, "Hold authority").on_hover_text("Repeat the desired state twice a second, as a master does (§11.2)").changed() {
                self.hold_authority(holding);
            }
        });

        edit::heading(ui, "Core operations");
        ui.horizontal(|ui| {
            for (mode, help) in [
                ("reset", "Clear a latched stop or fault and return to rest"),
                ("release", "Give up authority now"),
                ("hold", "Refresh authority, change nothing"),
            ] {
                if ui.button(mode).on_hover_text(help).clicked() {
                    if mode == "release" {
                        self.hold_authority(false);
                    }
                    self.send(ControlMessage::mode(mode));
                }
            }
        });
        if let Some((colour, note)) = &self.control_note {
            ui.add_space(6.0);
            ui.colored_label(*colour, note);
        }
    }

    fn state_tab(&self, ui: &mut Ui, def: &Definition) {
        let Some(s) = &self.state else {
            ui.spinner();
            return;
        };
        egui::Grid::new("state").num_columns(2).striped(true).show(ui, |ui| {
            let mut row = |k: &str, v: String| {
                ui.label(k);
                ui.label(v);
                ui.end_row();
            };
            row("Mode", def.label(&s.mode).to_string());
            row("Authority", s.authority.clone());
            row("Busy", s.busy.to_string());
            row("Fault", s.fault.clone().unwrap_or_else(|| "none".into()));
            row("Up", format!("{:.1} s", s.uptime_ms as f64 / 1000.0));
            if let Some(h) = s.free_heap {
                row("Free heap", format!("{h} bytes"));
            }
            if let Some(n) = &s.network {
                row("Network", n.clone());
            }
        });
        edit::heading(ui, "Device");
        egui::Grid::new("profile").num_columns(2).striped(true).show(ui, |ui| {
            for (k, v) in &s.profile {
                let label = ui.label(def.label(k));
                if let Some(decl) = def.object(k)
                    && let Some(doc) = edit::doc(def, decl) {
                        label.on_hover_text(doc);
                    }
                let text = match (v.as_str(), def.ui.get(k).and_then(|u| u.value_labels.as_ref())) {
                    (Some(s), Some(labels)) => labels.get(s).cloned().unwrap_or_else(|| s.to_string()),
                    _ => edit::show_value(v),
                };
                let unit = def.object(k).and_then(|d| d.unit.clone()).map(|u| format!(" {u}")).unwrap_or_default();
                ui.label(format!("{text}{unit}"));
                ui.end_row();
            }
        });
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.hold_authority(false);
    }
}

fn describe(d: &Detail) -> String {
    match d.reason.as_str() {
        "out_of_range" => match (&d.min, &d.max) {
            (Some(lo), Some(hi)) => format!("out of range ({lo} – {hi})"),
            _ => "out of range".into(),
        },
        "too_long" => format!("too long (max {})", d.max_len.unwrap_or(0)),
        "not_allowed" => format!("not one of {}", d.values.clone().unwrap_or_default().join(", ")),
        "requires_persist" => "must be stored".into(),
        other => other.replace('_', " "),
    }
}
