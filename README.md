# mrroip

MRRoIP ("Model **R**ail**R**oad over IP"): the protocol specification, a Python library to control
endpoints, and the conformance probe that checks them.

**The specification is a draft** (`MRROIP-1.md`). Wire details may still change; the implementations here
and in the [esp32](https://github.com/thetrainroom/esp32) applications are the only ones that exist. Apache-2.0 for the code, CC-BY-4.0 for the specification.

| Path | What |
|---|---|
| `MRROIP-1.md` | **The specification — a draft.** Core protocol: identity, discovery, description, configuration, control, state, authority. Sections 1–14 normative, conformance tests in Appendix A |
| `components/mrroip/` | ESP-IDF component: the endpoint core in C. The application adds the device profile |
| `src/mrroip/` | Python package: `Device`, discovery, display images and partial updates. Standard library only |
| `probe/mrroip_probe.py` | Core conformance suite C-1 … C-31 (§15) |
| `probe/mrroip_lib.py` | Probe harness: results, profile hooks; transport comes from the package |
| `probe/discovery_check.py` | What endpoints show on the network, and what happens across a boot (D-1 … D-11) |
| `examples/minimal_endpoint/` | ESP-IDF application: the core plus stub profile hooks — a starting point for a new device, and a compile test for every public header |
| `examples/send_image.py` | Show a picture, text or test pattern on a display endpoint |
| `examples/bounce.py` | A box bouncing on a colour endpoint: tile updates over HTTP, or whole frames over RTP |
| `examples/stream_ffmpeg.py` | Stream anything FFmpeg can read to a colour endpoint |
| `examples/clock.py` | An analog clock with a seconds dot on one or more displays, drawn without Pillow; fast-clock option (`--speed 4 --start 06:00`) |

Endpoints so far: the 1-bit SSD1306 display in `../esp32/oled` and the 240x280 colour display in `../esp32/colour`,
both in the sibling `esp32` repository. This repository holds only the parts that are not specific to one board.

## Use

Scripts in this repository find the package without installing it. To use it from anywhere:

```bash
python3.12 -m pip install -e .                 # from this repository; add .[image] for Pillow
```

```python
import mrroip

mrroip.ssdp_search()                    # {ip: headers} of endpoints that answer a search (§6.1)
mrroip.whois()                          # broadcast whois (§6.3)
mrroip.mdns_browse()                    # {ip: instance, host, port, TXT} for _mrroip._tcp (§6.2)
with mrroip.NotifyListener() as l:      # collects NOTIFY ssdp:alive / ssdp:byebye in the background
    ...                                 # l.events, l.since(t, ip, "ssdp:alive")
dev = mrroip.Device("192.168.10.164")
dev.definition()                        # objects, modes, parameters with ranges
dev.set_config(contrast=120, persist=True)
dev.control(mode="blink")               # desired state over HTTP ...
dev.control_udp(mode="show")            # ... or the same message over UDP
dev.state()

w, h = dev.image_size()
dev.show_image(mrroip.image.text("Gleis 3", w, h))
dev.show_image(mrroip.image.picture("logo.png", w, h), udp=True)
dev.update_image(frame)                 # only the rectangles that changed since the last update_image

w, h = dev.image_size()                                  # colour endpoints (plan question 16)
dev.put_image(mrroip.image.pattern_rgb565("bars", w, h)) # PUT /objects/image, rgb565be
dev.put_image(rect_pixels, x, y, w, h, base=image_id)    # a tile-aligned rectangle
dev.update_image_rgb565(frame)                           # only the tiles that changed
dev.control(mode="show", objects={"stream": {"port": 5004}})     # moving pictures: start
sender = mrroip.rtp.Sender(dev.ip, 5004, w, h, fps=5)            # RFC 4175 over RTP, paced, TAI timestamps
sender.send_frame(frame)
print(sender.sdp())                                              # for Wireshark "Decode As", ffplay, docs
mrroip.image.image_id(frame, w, h, 20)                   # what state.profile.image.id must report
dev.patch_image(crc, [(x, y, w, h, bytes)])   # rectangles on the image with checksum crc, by hand
dev.tx_bytes                            # request bytes sent so far
```

On a computer with several networks, SSDP searches and whois broadcasts leave through the default
interface. `mrroip.ssdp_search(iface="192.168.4.2")` sends from another local address; setting
`MRROIP_IFACE` does the same for code that does not pass it, such as the probe:
`MRROIP_IFACE=192.168.4.2 python3.12 probe/mrroip_probe.py --host 192.168.4.1 --only C-1`.

`seq` must grow per sender address (§9.5). `Device` counts from a millisecond timestamp, so several
programs on one computer stay in order. The probe counts from 1000 because two of its tests set `seq`
themselves; wait 5 seconds after other tools before running it, or its first messages count as replays.

## Firmware component (ESP-IDF)

`components/mrroip/` is the endpoint core for ESP-IDF 6.1: `/definition`, `/config`, `/control` over HTTP and UDP,
`/state`, authority and the control timeout, parameters in NVS, network bring-up (Ethernet first when configured,
otherwise Wi-Fi with a setup portal), SSDP, whois and mDNS. The device itself is a *profile* that the application
supplies: the functions declared in `include/mrroip_profile.h`. `../esp32/oled` is the first application.

In the application's `main/idf_component.yml`:

```yaml
dependencies:
  mrroip:
    git: git@github.com:thetrainroom/mrroip.git
    version: v0.2.0                 # a tag, a branch or a commit
    path: components/mrroip
```

The component manager clones it into the application's `managed_components/` and writes the exact
content hash into `dependencies.lock.<target>`, so a committed application rebuilds against the same
library code later. An application developed alongside this repository can point at the checkout
instead — `path: ../../../mrroip/components/mrroip` — which is what `../esp32/oled` and
`../esp32/colour` do; the two forms are exclusive, because a `git:` source ignores a local override
(`idf_component_tools/sources/__init__.py` tries `git` before `path`).

```c
#include "mrroip.h"

void app_main(void)
{
    mrroip_init();                          // NVS, flash writer, parameter table (core and profile)
    profile_start();                        // the application's profile
    mrroip_start(&(mrroip_config_t){ .ethernet = NULL });   // or the PHY pins, with CONFIG_MRROIP_ETHERNET
}
```

| Header | What for |
|---|---|
| `mrroip.h` | init and start, Ethernet pins, the `wifi` console command, config writes from a console, device id, restart |
| `mrroip_profile.h` | the functions a profile implements |
| `mrroip_params.h` | parameter descriptions; reading parameter values |
| `mrroip_emit.h`, `mrroip_value.h` | JSON out and in for the profile, without a JSON library (core §13.4) |
| `mrroip_net.h` | network state, e.g. for a status screen; Wi-Fi credentials |
| `mrroip_base64.h` | Base64 decoding for binary object states |

menuconfig → *MRRoIP*: `CONFIG_MRROIP_MDNS` (on by default, about 35 KB) and `CONFIG_MRROIP_ETHERNET`.
The settings that keep a Wi-Fi endpoint near 690 KB live in the application (`../esp32/oled/sdkconfig.defaults` and the
`wpa_supplicant` define in `../esp32/oled/CMakeLists.txt`; see `../esp32/oled/MRROIP-PLAN.md` §5).

## Probe

```bash
python3.12 probe/mrroip_probe.py --host 192.168.10.164 --no-prompt
python3.12 probe/mrroip_probe.py --discover
```

Profile tests and the hooks for C-16, C-21 and C-24 come from `probe/profile_<device_type>.py`
(for the display: milestone M5 of `../esp32/oled/MRROIP-PLAN.md`).

## Discovery check

`mrroip_probe.py` only *searches* (C-1 … C-4). `probe/discovery_check.py` also *listens*, and restarts the
device to see what it announces while booting.

```bash
python3.12 probe/discovery_check.py --scan
python3.12 probe/discovery_check.py --host 192.168.10.164 --reboot config --periodic
```

`--scan` lists every endpoint found by SSDP search, whois broadcast and mDNS, and flags any whose
`device_id` differs between them. With `--host` it prints the boot timeline (restart, `ssdp:byebye`,
HTTP back, `ssdp:alive` ×3) and runs:

| Test | Checks |
|---|---|
| D-1 | `ssdp:byebye` before a clean restart (only with `--reboot config`) |
| D-2 | the device restarts and HTTP answers again (recognised by `uptime_ms` starting over) |
| D-3 | three start-up `ssdp:alive`, and their spacing |
| D-4 | announcement headers: `X-MRROIP-*` match `/definition`, `NT`, `USN`, `max-age`, `LOCATION` fetches |
| D-5, D-6 | SSDP search for the MRRoIP target and for `ssdp:all` |
| D-7 | whois, unicast and broadcast |
| D-8, D-9 | mDNS host name; `_mrroip._tcp` service with TXT `id`, `name`, `type`, `class`, `fw` |
| D-10 | a rename is announced within 5 s, and search and mDNS show the new name |
| D-11 | a periodic announcement arrives (`--periodic`: `announce_interval_s` 30 for about 35 s) |

`--reboot`: `config` writes the parameter declared `"applies": "restart"` and restores it (two clean
restarts); `serial:PORT` resets through a serial port with auto-reset; `manual` asks you to pull the
power; `none` skips D-1 … D-4.

## Licence

Copyright (c) 2026 Thierry Gschwind.

| What | Licence | |
|---|---|---|
| The software: `components/mrroip/`, `src/mrroip/`, `probe/`, `examples/` | **Apache-2.0** | `LICENSE`, `NOTICE` |
| The specification: `MRROIP-1.md` | **CC-BY-4.0**, plus an explicit grant to implement | `LICENSE-SPEC` |

Apache-2.0 means you may put this component in a closed commercial product: keep the copyright
notice, pass on `NOTICE`, say if you changed the files, and nothing else is asked of you. It also
grants patent rights in both directions, which MIT does not.

Anyone may implement the protocol, for anything, with no permission or royalty — that is the point
of publishing it. The one thing reserved is the **name**: Apache-2.0 grants no trademark rights
(section 6), and `MRRoIP` is meant for implementations that pass the core conformance suite
(`MRROIP-1.md` Appendix A, `probe/mrroip_probe.py`). Build whatever you like on this code; call it
MRRoIP once it passes.
