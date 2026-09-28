// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! Holding authority (MRROIP-1.md §11.2): a master keeps it by repeating its desired state at 1 Hz or faster.
//! A `Keeper` does that over UDP from a background thread and reports every response, so a user interface
//! sees at once when another master takes over or the endpoint stops answering.

use std::net::{Ipv4Addr, UdpSocket};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use mrroip_proto::{ControlMessage, ControlResponse};

use crate::Device;

pub enum KeeperEvent {
    Reply(ControlResponse),
    /// No answer, or no valid one
    Silent(String),
}

enum Command {
    Desire(ControlMessage),
    Release,
}

pub struct Keeper {
    commands: Sender<Command>,
    thread: Option<JoinHandle<()>>,
}

impl Keeper {
    /// Starts repeating `desired` to `device` every `period`, reporting to `events`. The message is resent as
    /// it is — a desired state — with a fresh `seq` each time.
    pub fn start(device: Device, desired: ControlMessage, period: Duration, events: Sender<KeeperEvent>) -> std::io::Result<Keeper> {
        let (commands, inbox) = mpsc::channel();
        let socket = UdpSocket::bind((Ipv4Addr::UNSPECIFIED, 0))?;
        let mut device = device;
        device.timeout = period.min(Duration::from_secs(1));
        let thread = thread::Builder::new().name("mrroip-keeper".into()).spawn(move || run(device, socket, desired, period, inbox, events))?;
        Ok(Keeper { commands, thread: Some(thread) })
    }

    /// Replaces the desired state; it goes out at once
    pub fn desire(&self, msg: ControlMessage) {
        let _ = self.commands.send(Command::Desire(msg));
    }

    /// Sends `release` and stops (§9.3)
    pub fn release(mut self) {
        let _ = self.commands.send(Command::Release);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

impl Drop for Keeper {
    fn drop(&mut self) {
        let _ = self.commands.send(Command::Release);
    }
}

fn run(device: Device, socket: UdpSocket, mut desired: ControlMessage, period: Duration, inbox: Receiver<Command>, events: Sender<KeeperEvent>) {
    let send = |msg: &ControlMessage| {
        let fresh = ControlMessage { seq: 0, ts: None, ..msg.clone() };
        let event = match device.control_udp_on(&socket, fresh) {
            Ok(reply) => KeeperEvent::Reply(reply),
            Err(e) => KeeperEvent::Silent(e.to_string()),
        };
        events.send(event).is_ok()
    };
    loop {
        if !send(&desired) {
            return; // nobody is listening any more
        }
        match inbox.recv_timeout(period) {
            Ok(Command::Desire(msg)) => desired = msg,
            Ok(Command::Release) | Err(RecvTimeoutError::Disconnected) => {
                send(&ControlMessage::mode("release"));
                return;
            }
            Err(RecvTimeoutError::Timeout) => {}
        }
    }
}
