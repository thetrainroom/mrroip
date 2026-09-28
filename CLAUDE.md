# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MRRoIP ("Model RailRoad over IP"): one protocol, implemented several times in this repository, which must stay in agreement:

- `MRROIP-1.md` — the specification (a draft; CC-BY-4.0). Sections 1–14 are normative; Appendix A lists the conformance tests. Code comments cite it by section (`§9.5`, `§14 item 5`) — keep doing that, and update the spec when behaviour changes. The spec is allowed to follow the implementation where its text got in the way; surface such conflicts rather than silently picking a side.
- `components/mrroip/` — the endpoint core in C, an ESP-IDF 6.1 component (Apache-2.0).
- `src/mrroip/` — the Python package: the client (`Device`, discovery, images, RTP) and, in `src/mrroip/endpoint/`, an endpoint core for hosts (standard library only; Pillow optional via `.[image]`).
- `rust/` — Cargo workspace: `mrroip-proto` (wire types, the declaration grammar, SSDP messages), `mrroip-client` (Device, discovery, authority keeper), `mrroip-gui` (egui desktop app).
- `probe/` — the conformance probe that checks endpoints against the spec, built on the Python package.
- `conformance/` — the `reference` profile (`PROFILE-REFERENCE.md`, a lamp: off/on/blink) that every core ships; `vectors/*.json`, request→response cases every core's unit tests replay; and `grammar.json`, declaration→value→reason cases that the Python (`decl.py`) and Rust (`decl.rs`) checks both replay (formats in `conformance/README.md`).

The actual devices (boards, drivers, profiles) live in the sibling repository `../esp32` (`../esp32/oled`, `../esp32/colour`); they consume the component via `path: ../../../mrroip/components/mrroip`. Only board-independent code belongs here. Design notes referenced as "plan question N" or "milestone M5" are in `../esp32/oled/MRROIP-PLAN.md`.

## Commands

```bash
# Python: scripts find src/ without installing; to install anyway
python3.12 -m pip install -e .            # .[image] for Pillow
python3.12 -m unittest discover tests     # conformance vectors against the Python endpoint core
pyright                                    # strict for src/mrroip, standard for probe/tests/examples (config in pyproject.toml)

# A local endpoint (reference profile) to probe or to point the GUI at
PYTHONPATH=src python3.12 -m mrroip.endpoint --class stationary --http-port 8080 --state-dir /tmp/ep --device-id 02:00:00:00:00:01

# Rust
cd rust && cargo test --workspace && cargo clippy --workspace --all-targets
cargo run -p mrroip-gui
cargo run -p mrroip-client --example find            # like discovery_check.py --scan
cargo run -p mrroip-client --example smoke -- <ip:port>   # client round trip against a reference endpoint

# C: compile test of the component and every public header (needs the ESP-IDF environment)
cd examples/minimal_endpoint && idf.py set-target esp32c3 && idf.py build

# Conformance probe against an endpoint; ip:port for one not on port 80
python3.12 probe/mrroip_probe.py --host 192.168.10.164 --no-prompt
python3.12 probe/mrroip_probe.py --host <ip> --only C-11,P-15     # single tests; --skip, --core-only also exist
python3.12 probe/discovery_check.py --scan
python3.12 probe/discovery_check.py --host <ip> --reboot config --periodic   # D-1 … D-11
```

- Probe a local endpoint by its LAN address, not `127.0.0.1`: SSDP answers come from the LAN address, and C-1 matches on it.
- `MRROIP_IFACE=<local ip>` picks the interface for SSDP/whois broadcasts on multi-homed machines (Python and Rust alike).
- `seq` must grow per sender address (§9.5); the probe counts from 1000, so wait ~5 s after other tools before running it or its first messages are rejected as replays.
- C-28 (`--slow`) and C-31 (`--soak`) take ~30 min and are off by default.

## Architecture

**Core vs. profile.** Every core implements everything device-independent: `/definition`, `/config`, `/control` (HTTP and UDP), `/state`, `PUT /objects/<id>`, authority and control timeout, stored parameters, SSDP, whois, mDNS. The device itself is a *profile*: in C the functions of `include/mrroip_profile.h`, in Python a `mrroip.endpoint.Profile` subclass with the same methods, declaring its parameters as `Param` dataclasses (`Param.decl()` is the wire declaration that both `/definition` and validation use). The core never names a profile parameter or object; the profile never parses JSON or touches a socket. C profiles emit JSON through `mrroip_emit.h` and read values through `mrroip_value.h`. Optional C hooks (`profile_object_stream_*`, `profile_resume`) have weak defaults in `src/profile_defaults.c`.

**The cores mirror each other.** `src/mrroip/endpoint/params.py` and `control.py` follow `components/mrroip/src/params.c` and `control.c` check for check, in the same order, because the order decides which error a message gets. A behaviour change in one core means the same change in the others, a vector in `conformance/vectors/`, and the spec. `endpoint/core.py` has no sockets (the vectors drive it with a fake clock); `endpoint/server.py` puts it on the network.

When adding a source file to the C component, list it in `components/mrroip/CMakeLists.txt` `SRCS`. Public headers go in `include/` and must compile standalone — `examples/minimal_endpoint/main/h_*.c` includes each one alone; add an `h_*.c` for a new public header.

**Clients.** Python: `mrroip.Device` wraps the HTTP/UDP API (address `ip` or `ip:port`); `discovery.py` does SSDP/whois/mDNS/NOTIFY; `image.py` and `rtp.py` handle display payloads. Rust: `mrroip-client` is the same surface, non-blocking (discovery reports as it finds), plus `Keeper`, which repeats a desired state to hold authority. `mrroip-proto::Decl::check` and `src/mrroip/decl.py` validate a value against a declaration identically — the GUI uses one, the Python endpoint the other. The GUI renders everything from `/definition` (labels, groups, `advanced` from its `ui` section) and knows no device type.

**Probe.** `probe/mrroip_lib.py` re-exports from the package and adds result plumbing and `Hooks`. Core tests (C-*) are in `mrroip_probe.py`; profile tests (P-*) and hooks for C-16/C-21/C-22/C-24 live in `probe/profile_<device_type>.py`, loaded by the endpoint's reported `device_type` (`profile_reference.py`, `profile_display.py`).

**Protocol name in one place per language.** The name, token, spec version, ports, SSDP ST and header prefix are spelled only in `components/mrroip/src/proto_name.h`, `src/mrroip/protocol.py` and `rust/mrroip-proto/src/protocol.rs`, mirroring spec §2.4. Change them together.

## Conventions

- Versioning: one number shared by `pyproject.toml`, `src/mrroip/__init__.py` (`__version__`), `components/mrroip/idf_component.yml`, the commented `version:` in `examples/minimal_endpoint/main/idf_component.yml`, `rust/Cargo.toml` (`workspace.package.version`), the README's example, and the git tag. Bump them together.
- Python: 3.12 or later. `src/mrroip` is fully annotated and must stay clean under pyright strict (it ships `py.typed`). IP addresses are `ipaddress.IPv4Address`, never `str`; convert to text only at the wire (JSON fields, socket calls, URLs). Fixed shapes are dataclasses (`Param`, `Response`, `WhoisReply`, `MdnsService`, `NotifyEvent`); protocol documents that are open by design (`state.profile`, `endpoints`, `ui`, SSDP headers) stay dicts, and the probe inspects raw JSON on purpose. JSON documents are `JsonObject`/`Json` from `mrroip/_types.py`; narrow them with `as_object()`/`as_list()`, not bare `isinstance`.
- Every source file starts with the SPDX header (`SPDX-FileCopyrightText: 2026 Thierry Gschwind`, `SPDX-License-Identifier: Apache-2.0`).
- `idf_component.yml` pins `espressif/mdns` exactly on purpose (applications with several targets share `managed_components/`, so their locks must agree).
- Build output (`build/`, `rust/target/`), `sdkconfig` and `dependencies.lock` are not committed.
