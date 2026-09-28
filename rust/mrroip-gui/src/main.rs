// SPDX-FileCopyrightText: 2026 Thierry Gschwind
// SPDX-License-Identifier: Apache-2.0
//! MRRoIP desktop app: find endpoints on the network, read what they are, configure and command them.
//! Everything it shows comes from each endpoint's `/definition`; it knows no device type.

mod app;
mod edit;
mod view;

fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: eframe::egui::ViewportBuilder::default().with_inner_size([1100.0, 700.0]).with_min_inner_size([700.0, 400.0]),
        ..Default::default()
    };
    eframe::run_native("MRRoIP", options, Box::new(|cc| Ok(Box::new(app::App::new(cc)))))
}
