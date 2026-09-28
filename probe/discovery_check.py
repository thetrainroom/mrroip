#!/usr/bin/env python3
"""
discovery_check.py — what MRRoIP endpoints show on the network, and what happens across a boot
(MRROIP-CORE-SPEC.md §6). mrroip_probe.py's C-1…C-4 only search; this also listens.

    python3.12 discovery_check.py --scan
    python3.12 discovery_check.py --host 192.168.10.164 --reboot config
    python3.12 discovery_check.py --host 192.168.10.164 --reboot serial:/dev/cu.usbserial-8320
    python3.12 discovery_check.py --host 192.168.10.164 --reboot manual
    python3.12 discovery_check.py --host 192.168.10.164 --reboot none --periodic

--scan        lists every endpoint found by SSDP search, whois broadcast and mDNS, and whether they agree.
--reboot      how to restart the device while listening:
  config      write a parameter declared "applies": "restart" (the display's `panel`) with persist, then
              restore it: two clean restarts, so ssdp:byebye is expected
  serial:PORT hard reset by opening a serial port with auto-reset (ESP32 kits): no byebye
  manual      you pull the power when asked: no byebye
  none        no restart; only the tests that do not need one
--periodic    also wait for a periodic announcement (sets announce_interval_s to 30 for about 35 s)
--iface ADDR  local address to send and listen from, on a computer with several networks
"""

import argparse
import socket
import sys
import time
import urllib.request

from mrroip_lib import Res, mrroip            # mrroip_lib makes the package importable
from mrroip.discovery import NotifyListener, mdns_browse, ssdp_search, whois
from mrroip.protocol import HEADER_PREFIX, SSDP_ST, TOKEN

ALIVE, BYEBYE = "ssdp:alive", "ssdp:byebye"
STARTUP_ANNOUNCEMENTS = 3


# ------------------------------------------------------------------ helpers

def uptime_ms(dev):
    """The endpoint's uptime from /state, or None if it does not answer."""
    try:
        code, state = dev.req("/state")
        return state.get("uptime_ms") if code == 200 else None
    except Exception:
        return None


def wait_until(pred, limit, poll=0.25):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        if pred():
            return time.monotonic()
        time.sleep(poll)
    return None


def fmt(t, t0):
    return f"{t - t0:+7.2f} s" if t is not None else "     —   "


# ------------------------------------------------------------------ restart methods

def restart_config(dev, parameter, value):
    """Clean restart through a restart-applied parameter. Returns the trigger time."""
    t = time.monotonic()
    code, body = dev.set_config(**{parameter: value, "persist": True})
    if code != 200:
        raise SystemExit(f"writing {parameter}={value} returned {code}: {body}")
    return t


def restart_serial(port):
    import serial
    s = serial.Serial(port, 115200, timeout=0.1)       # opening the port pulses the auto-reset circuit
    t = time.monotonic()
    time.sleep(0.5)
    s.close()
    return t


def observe_boot(dev, listener, trigger, label):
    """
    Restart via trigger() and record the timeline. Returns a dict of times (monotonic) and events.

    A restart is recognised by uptime_ms starting over, not by missed polls alone: a fast boot can fall
    between two polls, and a request sent to a restarting device can hang until it is back.
    """
    print(f"\n--- boot: {label}")
    fast = mrroip.Device(dev.ip, dev.udp_port, timeout=0.5)
    before, t_before = uptime_ms(fast), time.monotonic()
    t0 = trigger()
    down = up = None
    while time.monotonic() - t0 < 90:
        u, now = uptime_ms(fast), time.monotonic()
        if u is None:
            down = down or now
        elif before is not None and u < before + (now - t_before) * 1000 - 2000:
            up = now                                # uptime started over: this is the new boot
            break
        time.sleep(0.2)
    if up:
        # announcements go out about a second after the network is up; give them time
        wait_until(lambda: len(listener.since(t0, dev.ip, ALIVE)) >= STARTUP_ANNOUNCEMENTS, 10, poll=0.2)
        time.sleep(1.0)
    byes = listener.since(t0 - 2.0, dev.ip, BYEBYE)
    alives = listener.since(t0, dev.ip, ALIVE)
    timeline = [(t0, "restart triggered")] + [(e["t"], "ssdp:byebye") for e in byes]
    timeline += [(down, "HTTP stops answering"), (up, "HTTP answers again, uptime started over")]
    timeline += [(e["t"], f"ssdp:alive #{i + 1}") for i, e in enumerate(alives)]
    for t, what in sorted((x for x in timeline if x[0] is not None), key=lambda x: x[0]):
        print(f"  {fmt(t, t0)}  {what}")
    return {"t0": t0, "down": down, "up": up, "byebye": byes, "alive": alives}


# ------------------------------------------------------------------ tests

def d01_byebye(boot, method):
    r = Res("D-1", "ssdp:byebye before a clean restart")
    if method != "config":
        return r.skipped(f"{method} is a hard reset: nothing can be sent")
    if not boot["byebye"]:
        return r.failed("no ssdp:byebye seen")
    lead = (boot["down"] - boot["byebye"][0]["t"]) if boot["down"] else None
    return r.passed(f"{len(boot['byebye'])} seen" + (f", {lead:.2f} s before HTTP stopped" if lead is not None else ""))


def d02_back(boot):
    r = Res("D-2", "HTTP answers again after boot")
    if not boot["up"]:
        return r.failed("no restart seen within 90 s: uptime_ms never started over")
    note = f"{boot['up'] - boot['t0']:.1f} s after the trigger"
    if boot["down"]:
        note += f", unreachable for {boot['up'] - boot['down']:.1f} s"
    return r.passed(note)


def d03_startup(boot):
    r = Res("D-3", f"{STARTUP_ANNOUNCEMENTS} start-up announcements")
    alives = boot["alive"]
    if len(alives) < STARTUP_ANNOUNCEMENTS:
        return r.failed(f"{len(alives)} ssdp:alive seen after the restart, §6.1 wants {STARTUP_ANNOUNCEMENTS}")
    first = alives[:STARTUP_ANNOUNCEMENTS]
    gaps = [round((b["t"] - a["t"]) * 1000) for a, b in zip(first, first[1:])]
    after_up = (first[0]["t"] - boot["up"]) if boot["up"] else None
    return r.passed(f"gaps {gaps} ms" + (f", first {after_up:+.2f} s from HTTP up" if after_up is not None else ""))


def d04_headers(dev, alive):
    r = Res("D-4", "announcement headers")
    if not alive:
        return r.skipped("no announcement to inspect")
    h, dfn, bad = alive[-1]["headers"], dev.dfn, []
    for suffix, key in (("ID", "device_id"), ("NAME", "device_name"), ("TYPE", "device_type"), ("CLASS", "device_class")):
        if h.get(HEADER_PREFIX + suffix) != dfn.get(key):
            bad.append(f"{HEADER_PREFIX}{suffix}={h.get(HEADER_PREFIX + suffix)!r} != {dfn.get(key)!r}")
    if h.get("NT") != SSDP_ST:
        bad.append(f"NT {h.get('NT')!r}")
    usn = h.get("USN", "")
    if TOKEN not in usn.lower() or dfn["device_id"].replace(":", "") not in usn.lower():
        bad.append(f"USN {usn!r} lacks the token or the MAC")
    if "max-age" not in h.get("CACHE-CONTROL", ""):
        bad.append("no CACHE-CONTROL max-age")
    location = h.get("LOCATION", "")
    try:
        with urllib.request.urlopen(location, timeout=4) as f:
            import json
            if json.loads(f.read()).get("device_id") != dfn["device_id"]:
                bad.append(f"LOCATION {location} belongs to another device")
    except Exception as e:
        bad.append(f"LOCATION {location!r} did not fetch: {e}")
    return r.failed("; ".join(bad)) if bad else r.passed(f"LOCATION {location}, {h.get('CACHE-CONTROL')}")


def d05_search(dev, iface):
    r = Res("D-5", "SSDP search for the MRRoIP target")
    t = time.monotonic()
    hits = ssdp_search(timeout=5.0, iface=iface)
    if dev.ip not in hits:
        return r.failed(f"no answer within 5 s (found: {sorted(hits)})")
    return r.passed(f"{len(hits)} endpoint(s) answered")


def d06_search_all(dev, iface):
    r = Res("D-6", "SSDP search for ssdp:all")
    hits = ssdp_search(timeout=5.0, st="ssdp:all", iface=iface)
    if dev.ip not in hits:
        return r.failed("no answer to ssdp:all")
    st = hits[dev.ip].get("ST")
    return r.passed(f"answers with ST {st}") if st == SSDP_ST else r.failed(f"answer carries ST {st!r}")


def d07_whois(dev, iface):
    r = Res("D-7", "whois, unicast and broadcast")
    uni = whois(dev.ip, iface=iface).get(dev.ip, {})
    broad = whois(iface=iface).get(dev.ip, {})
    problems = []
    for label, reply in (("unicast", uni), ("broadcast", broad)):
        if not reply:
            problems.append(f"no {label} reply")
        elif reply.get("id") != dev.dfn["device_id"] or reply.get("ip") != dev.ip:
            problems.append(f"{label} reply id={reply.get('id')} ip={reply.get('ip')}")
    return r.failed("; ".join(problems)) if problems else r.passed(f"both answered as {uni.get('name')!r} at {uni.get('ip')}")


def d08_mdns_host(dev):
    r = Res("D-8", "mDNS host name")
    name = f"{dev.dfn['device_name']}.local"
    try:
        ip = socket.gethostbyname(name)
    except Exception as e:
        return r.failed(f"{name} did not resolve: {e}")
    return r.passed(f"{name} -> {ip}") if ip == dev.ip else r.failed(f"{name} -> {ip}, expected {dev.ip}")


def d09_mdns_service(dev, iface):
    r = Res("D-9", "mDNS service and TXT records")
    found = mdns_browse(iface=iface).get(dev.ip)
    if not found:
        return r.failed("the _mrroip._tcp browse found nothing at this address")
    txt, dfn, bad = found["txt"], dev.dfn, []
    for key, field in (("id", "device_id"), ("name", "device_name"), ("type", "device_type"),
                       ("class", "device_class"), ("fw", "firmware")):
        if txt.get(key) != dfn.get(field):
            bad.append(f"TXT {key}={txt.get(key)!r} != {dfn.get(field)!r}")
    if found["port"] != 80:
        bad.append(f"port {found['port']}")
    return r.failed("; ".join(bad)) if bad else r.passed(f"{found['instance']} on {found['host']}:{found['port']}")


def d10_rename(dev, listener, iface):
    r = Res("D-10", "a rename is announced at once")
    original, new = dev.dfn["device_name"], "discovery-check"
    t = time.monotonic()
    code, _ = dev.set_config(device_name=new, persist=False)
    if code != 200:
        return r.failed(f"rename returned {code}")
    try:
        seen = wait_until(lambda: any(e["headers"].get(HEADER_PREFIX + "NAME") == new
                                      for e in listener.since(t, dev.ip, ALIVE)), 5, poll=0.1)
        search_name = ssdp_search(timeout=4.0, iface=iface).get(dev.ip, {}).get(HEADER_PREFIX + "NAME")
        mdns_name = (mdns_browse(iface=iface).get(dev.ip) or {}).get("txt", {}).get("name")
    finally:
        dev.set_config(device_name=original, persist=False)
    problems = []
    if not seen:
        problems.append("no ssdp:alive with the new name within 5 s")
    if search_name != new:
        problems.append(f"search still answers as {search_name!r}")
    if mdns_name != new:
        problems.append(f"mDNS TXT name is {mdns_name!r}")
    return r.failed("; ".join(problems)) if problems else r.passed(f"announced after {seen - t:.2f} s; search and mDNS agree")


def d11_periodic(dev, listener):
    r = Res("D-11", "periodic announcements")
    original = dev.cfg()["announce_interval_s"]
    t = time.monotonic()
    code, _ = dev.set_config(announce_interval_s=30, persist=False)
    if code != 200:
        return r.failed(f"announce_interval_s=30 returned {code}")
    try:
        seen = wait_until(lambda: listener.since(t + 1.0, dev.ip, ALIVE), 40, poll=0.5)
    finally:
        dev.set_config(announce_interval_s=original, persist=False)
    return r.passed(f"ssdp:alive {seen - t:.1f} s after setting 30 s") if seen else r.failed("nothing within 40 s")


# ------------------------------------------------------------------ scan

def scan(iface):
    print("SSDP search, whois broadcast and mDNS browse ...")
    ssdp = ssdp_search(timeout=5.0, iface=iface)
    who = whois(iface=iface)
    mdns = mdns_browse(iface=iface)
    ips = sorted(set(ssdp) | set(who) | set(mdns), key=lambda ip: tuple(int(x) for x in ip.split(".")))
    if not ips:
        print("Nothing found. If a device is up, multicast and broadcast are probably blocked here.")
        return 1
    print(f"\n{'address':16s} {'name':18s} {'type':10s} {'class':11s} SSDP  whois  mDNS  device_id")
    status = 0
    for ip in ips:
        s, w, m = ssdp.get(ip, {}), who.get(ip, {}), (mdns.get(ip) or {}).get("txt", {})
        ids = {x for x in (s.get(HEADER_PREFIX + "ID"), w.get("id"), m.get("id")) if x}
        name = s.get(HEADER_PREFIX + "NAME") or w.get("name") or m.get("name") or "?"
        kind = s.get(HEADER_PREFIX + "TYPE") or w.get("type") or m.get("type") or "?"
        cls = s.get(HEADER_PREFIX + "CLASS") or w.get("class") or m.get("class") or "?"
        mark = lambda present: " yes " if present else "  —  "
        agree = "" if len(ids) <= 1 else "   <- mechanisms disagree"
        status |= 1 if agree else 0
        print(f"{ip:16s} {name[:18]:18s} {kind[:10]:10s} {cls[:11]:11s} {mark(s)} {mark(w)}  {mark(m)} "
              f"{', '.join(sorted(ids))}{agree}")
    return status


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--host")
    ap.add_argument("--reboot", default="none", help="config | serial:PORT | manual | none")
    ap.add_argument("--periodic", action="store_true")
    ap.add_argument("--iface")
    args = ap.parse_args()

    if args.scan or not args.host:
        return scan(args.iface)

    dev = mrroip.Device(args.host)
    dfn = dev.definition()
    print(f"{dfn['device_name']}  {dfn['device_id']}  {dfn['device_type']}/{dfn['device_class']}  fw {dfn['firmware']}")
    method = args.reboot.split(":", 1)[0]
    results = []

    with NotifyListener(iface=args.iface) as listener:
        time.sleep(0.5)
        if method == "config":
            p = next((p for p in dfn["parameters"] if p.get("applies") == "restart" and p.get("values")), None)
            if not p:
                raise SystemExit("no parameter declared applies: restart with values; use --reboot serial:PORT or manual")
            original = dev.cfg()[p["name"]]
            other = next(v for v in p["values"] if v != original)
            first = observe_boot(dev, listener, lambda: restart_config(dev, p["name"], other), f"{p['name']}={other}")
            if not first["up"]:
                dev.set_config(**{p["name"]: original, "persist": True})     # never leave it on the wrong value
                raise SystemExit(f"the device did not come back after the first restart; {p['name']} restored to {original}")
            boot = observe_boot(dev, listener, lambda: restart_config(dev, p["name"], original), f"{p['name']}={original} (measured)")
        elif method == "serial":
            boot = observe_boot(dev, listener, lambda: restart_serial(args.reboot.split(":", 1)[1]), "hard reset via serial")
        elif method == "manual":
            def trigger():
                input("    >>> power-cycle the device, then press enter as soon as it is off ")
                return time.monotonic()
            boot = observe_boot(dev, listener, trigger, "manual power cycle")
        else:
            boot = None

        if boot:
            dev.definition()
            results += [d01_byebye(boot, method), d02_back(boot), d03_startup(boot), d04_headers(dev, boot["alive"])]
            if not boot["up"]:
                return report(results)
        results += [d05_search(dev, args.iface), d06_search_all(dev, args.iface), d07_whois(dev, args.iface),
                    d08_mdns_host(dev), d09_mdns_service(dev, args.iface), d10_rename(dev, listener, args.iface)]
        if args.periodic:
            results.append(d11_periodic(dev, listener))
        else:
            results.append(Res("D-11", "periodic announcements").skipped("pass --periodic (about 35 s)"))
    return report(results)


def report(results):
    print("\n" + "=" * 78)
    for r in results:
        print(f"  {r.id:5s} {({True: ' ok ', False: 'FAIL', None: 'skip'})[r.ok]}  {r.name:<38s} {r.note[:60]}")
    print("=" * 78)
    passed = sum(r.ok is True for r in results)
    failed = sum(r.ok is False for r in results)
    print(f"{passed} passed, {failed} failed, {sum(r.ok is None for r in results)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
