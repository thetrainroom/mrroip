# mmroip

MMRoIP ("Model Railroad over IP"): the protocol specification, a Python library to control endpoints,
and the conformance probe that checks them.

| Path | What |
|---|---|
| `MMROIP-CORE-SPEC.md` | Core endpoint specification (rev 1.0) |
| `src/mmroip/` | Python package: `Device`, discovery, display images. Standard library only |
| `probe/mmroip_probe.py` | Core conformance suite C-1 … C-31 (§15) |
| `probe/mmroip_lib.py` | Probe harness: results, profile hooks; transport comes from the package |
| `examples/send_image.py` | Show a picture, text or test pattern on a display endpoint |

The first endpoint is the SSD1306 display in `../oled`.

## Use

Scripts in this folder find the package without installing it. To use it from anywhere:

```bash
python3.12 -m pip install -e mmroip            # from the repository root; add [image] for Pillow
```

```python
import mmroip

mmroip.ssdp_search()                    # {ip: headers} of endpoints that announce themselves (§6.1)
dev = mmroip.Device("192.168.10.164")
dev.definition()                        # objects, modes, parameters with ranges
dev.set_config(contrast=120, persist=True)
dev.control(mode="blink")               # desired state over HTTP ...
dev.control_udp(mode="show")            # ... or the same message over UDP
dev.state()

w, h = dev.image_size()
dev.show_image(mmroip.image.text("Gleis 3", w, h))
dev.show_image(mmroip.image.picture("logo.png", w, h), udp=True)
```

On a computer with several networks, SSDP searches and whois broadcasts leave through the default
interface. `mmroip.ssdp_search(iface="192.168.4.2")` sends from another local address; setting
`MMROIP_IFACE` does the same for code that does not pass it, such as the probe:
`MMROIP_IFACE=192.168.4.2 python3.12 probe/mmroip_probe.py --host 192.168.4.1 --only C-1`.

`seq` must grow per sender address (§9.5). `Device` counts from a millisecond timestamp, so several
programs on one computer stay in order. The probe counts from 1000 because two of its tests set `seq`
themselves; wait 5 seconds after other tools before running it, or its first messages count as replays.

## Probe

```bash
python3.12 probe/mmroip_probe.py --host 192.168.10.164 --no-prompt
python3.12 probe/mmroip_probe.py --discover
```

Profile tests and the hooks for C-16, C-21 and C-24 come from `probe/profile_<device_type>.py`
(for the display: milestone M5 of `../oled/MMROIP-PLAN.md`).
