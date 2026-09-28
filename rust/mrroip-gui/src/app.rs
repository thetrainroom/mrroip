// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! The window: the device list on the left, the selected device on the right. Every network call runs on a
//! background thread and reports back through one channel, so the interface never waits for a device.

use std::collections::{BTreeMap, BTreeSet};
use std::net::Ipv4Addr;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;
use std::time::{Duration, Instant};

use eframe::egui::{self, Color32, RichText, Ui};
use mrroip_client::discovery::{self, Found, NotifyListener, Via};
use mrroip_client::{ConfigOutcome, Device, KeeperEvent};
use mrroip_proto::{ConfigReply, ControlResponse, Definition, State};

use crate::view::Session;

/// What a background thread reports
pub enum Msg {
    Found(Found),
    ScanDone,
    Loaded(String, Result<(Definition, ConfigReply), String>),
    Config(String, Result<ConfigOutcome, String>),
    State(String, Result<State, String>),
    Control(String, Result<(u16, ControlResponse), String>),
    Keeper(String, KeeperEvent),
}

/// A sender that also wakes the interface
#[derive(Clone)]
pub struct Tx {
    tx: Sender<Msg>,
    ctx: egui::Context,
}

impl Tx {
    pub fn send(&self, msg: Msg) {
        let _ = self.tx.send(msg);
        self.ctx.request_repaint();
    }

    /// Runs `job` on its own thread and sends what it returns
    pub fn spawn(&self, job: impl FnOnce() -> Msg + Send + 'static) {
        let tx = self.clone();
        thread::spawn(move || tx.send(job()));
    }
}

/// An endpoint in the list
pub struct Entry {
    pub ip: Ipv4Addr,
    pub http_port: u16,
    pub name: Option<String>,
    pub device_type: Option<String>,
    pub device_class: Option<String>,
    pub via: BTreeSet<Via>,
    last_seen: Instant,
    max_age: Duration,
    gone: bool,
}

impl Entry {
    pub fn online(&self) -> bool {
        !self.gone && self.last_seen.elapsed() < self.max_age
    }

    pub fn addr(&self) -> String {
        if self.http_port == 80 { self.ip.to_string() } else { format!("{}:{}", self.ip, self.http_port) }
    }
}

pub struct App {
    tx: Tx,
    rx: Receiver<Msg>,
    entries: BTreeMap<String, Entry>,
    iface: String,
    manual: String,
    scanning: usize,
    listener: Option<NotifyListener>,
    status: String,
    session: Option<Session>,
}

impl App {
    pub fn new(cc: &eframe::CreationContext<'_>) -> App {
        let (tx, rx) = mpsc::channel();
        let tx = Tx { tx, ctx: cc.egui_ctx.clone() };
        let iface = discovery::default_iface().map(|i| i.to_string()).unwrap_or_default();
        let mut app = App {
            tx,
            rx,
            entries: BTreeMap::new(),
            iface,
            manual: String::new(),
            scanning: 0,
            listener: None,
            status: String::new(),
            session: None,
        };
        app.listen();
        app.scan();
        app
    }

    fn iface(&self) -> Option<Ipv4Addr> {
        self.iface.trim().parse().ok()
    }

    /// Hears `ssdp:alive` and `ssdp:byebye` for as long as the app runs
    fn listen(&mut self) {
        let tx = self.tx.clone();
        match NotifyListener::start(self.iface(), move |f| tx.send(Msg::Found(f))) {
            Ok(l) => self.listener = Some(l),
            Err(e) => self.status = format!("not listening for announcements: {e}"),
        }
    }

    fn scan(&mut self) {
        let iface = self.iface();
        type Search = fn(Option<Ipv4Addr>, &mut dyn FnMut(Found)) -> std::io::Result<()>;
        let searches: [Search; 3] = [
            |i, f| discovery::ssdp_search(Duration::from_secs(3), i, f),
            |i, f| discovery::whois(None, Duration::from_secs(2), i, f),
            |i, f| discovery::mdns_browse(Duration::from_secs(2), i, f),
        ];
        for search in searches {
            self.scanning += 1;
            let tx = self.tx.clone();
            thread::spawn(move || {
                let mut report = |f| tx.send(Msg::Found(f));
                let _ = search(iface, &mut report);
                tx.send(Msg::ScanDone);
            });
        }
    }

    fn found(&mut self, f: Found) {
        let key = f.device_id.clone().unwrap_or_else(|| f.ip.to_string());
        let entry = self.entries.entry(key).or_insert_with(|| Entry {
            ip: f.ip,
            http_port: f.http_port,
            name: None,
            device_type: None,
            device_class: None,
            via: BTreeSet::new(),
            last_seen: Instant::now(),
            max_age: Duration::from_secs(600),
            gone: false,
        });
        if !f.alive {
            entry.gone = true;
            return;
        }
        entry.ip = f.ip;
        entry.http_port = f.http_port;
        entry.name = f.name.or(entry.name.take());
        entry.device_type = f.device_type.or(entry.device_type.take());
        entry.device_class = f.device_class.or(entry.device_class.take());
        entry.via.insert(f.via);
        entry.last_seen = Instant::now();
        entry.max_age = f.max_age.unwrap_or(Duration::from_secs(600));
        entry.gone = false;
    }

    fn open(&mut self, key: String, addr: &str) {
        match Device::new(addr) {
            Ok(device) => self.session = Some(Session::open(key, device, self.tx.clone())),
            Err(e) => self.status = format!("{addr}: {e}"),
        }
    }

    fn drain(&mut self) {
        while let Ok(msg) = self.rx.try_recv() {
            match msg {
                Msg::Found(f) => self.found(f),
                Msg::ScanDone => self.scanning = self.scanning.saturating_sub(1),
                other => {
                    if let Some(s) = &mut self.session {
                        s.receive(other);
                    }
                }
            }
        }
    }

    fn device_list(&mut self, ui: &mut Ui) {
        ui.horizontal(|ui| {
            ui.label("Interface");
            ui.add(egui::TextEdit::singleline(&mut self.iface).hint_text("default").desired_width(110.0))
                .on_hover_text("The local IPv4 address to search from, on a computer with several networks");
            let busy = self.scanning > 0;
            if ui.add_enabled(!busy, egui::Button::new(if busy { "Searching…" } else { "Search" })).clicked() {
                self.listener = None;
                self.listen();
                self.scan();
            }
        });
        ui.horizontal(|ui| {
            ui.label("Address");
            let edit = ui.add(egui::TextEdit::singleline(&mut self.manual).hint_text("ip or ip:port").desired_width(130.0));
            let go = ui.button("Open").clicked() || (edit.lost_focus() && ui.input(|i| i.key_pressed(egui::Key::Enter)));
            if go && !self.manual.trim().is_empty() {
                let addr = self.manual.trim().to_string();
                self.open(addr.clone(), &addr);
            }
        });
        if !self.status.is_empty() {
            ui.colored_label(Color32::from_rgb(200, 120, 0), &self.status);
        }
        ui.separator();

        if self.entries.is_empty() && self.scanning == 0 {
            ui.label("No endpoint answered. If one is running, multicast may be blocked between this computer \
                      and it (a guest network or a mesh access point does that); open it by address.");
        }
        let selected = self.session.as_ref().map(|s| s.key.clone());
        let mut clicked = None;
        egui::ScrollArea::vertical().show(ui, |ui| {
            for (key, e) in &self.entries {
                let dot = if e.online() { RichText::new("●").color(Color32::from_rgb(40, 170, 70)) } else { RichText::new("○").weak() };
                let name = e.name.clone().unwrap_or_else(|| e.addr());
                let text = format!("{name}\n{}  {}  {}", e.device_type.as_deref().unwrap_or("?"), e.device_class.as_deref().unwrap_or(""), e.addr());
                ui.horizontal(|ui| {
                    ui.label(dot);
                    let via: Vec<String> = e.via.iter().map(|v| format!("{v:?}")).collect();
                    if ui.selectable_label(selected.as_deref() == Some(key.as_str()), text).on_hover_text(format!("{key}\nfound by {}", via.join(", "))).clicked() {
                        clicked = Some((key.clone(), e.addr()));
                    }
                });
            }
        });
        if let Some((key, addr)) = clicked {
            self.open(key, &addr);
        }
    }
}

impl eframe::App for App {
    fn ui(&mut self, ui: &mut Ui, _frame: &mut eframe::Frame) {
        self.drain();
        egui::Panel::left("devices").resizable(true).default_size(280.0).show(ui, |ui| self.device_list(ui));
        egui::CentralPanel::default().show(ui, |ui| match &mut self.session {
            Some(s) => s.ui(ui),
            None => {
                ui.centered_and_justified(|ui| ui.label("Select an endpoint"));
            }
        });
        // online/offline follows max-age even when nothing arrives
        ui.ctx().request_repaint_after(Duration::from_secs(1));
    }
}

/// Stops a polling thread when dropped
pub struct StopFlag(pub Arc<AtomicBool>);

impl Drop for StopFlag {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Relaxed);
    }
}
