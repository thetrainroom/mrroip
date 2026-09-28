#!/usr/bin/env python3
"""
mrroip_probe.py — core conformance suite for an MRRoIP endpoint.

Implements the C-tests of MRROIP-CORE-SPEC.md §15. Nothing here knows what kind
of device it is talking to; profile behaviour is tested by profile_<type>.py,
which this driver loads automatically from /definition.device_type.

    python3 mrroip_probe.py --discover
    python3 mrroip_probe.py --host 192.168.1.47
    python3 mrroip_probe.py --host 192.168.1.47 --core-only
    python3 mrroip_probe.py --host 192.168.1.47 --only C-11,C-16,C-21,C-22
    python3 mrroip_probe.py --host 192.168.1.47 --profile cablecar --soak

C-11, C-28, C-30 and C-31 need an operator (a power cycle, patience, an SSID
change). They prompt; --no-prompt skips them.
"""

import argparse, importlib, json, socket, sys, time

from mrroip_lib import (Res, test, Dev, Hooks, ssdp_search, whois, wait_back, ask,
                        CORE_MODES)

REQUIRED_TOP = ("proto", "proto_version", "device_id", "device_name", "device_type",
                "device_class", "profile_version", "firmware", "endpoints",
                "capabilities", "parameters")
CORE_PARAMS  = ("device_name", "control_timeout_ms", "udp_port", "announce_interval_s")
CLASSES      = ("mobile", "stationary", "passive")


# ---------------------------------------------------------------- discovery

@test("C-1", "SSDP discovery")
def c01(d, ctx):
    r = Res("C-1", c01._name)
    hits = ssdp_search(timeout=5.0)
    if d.ip not in hits:
        return r.failed("no SSDP response within 5 s")
    ctx["ssdp"] = hits[d.ip]
    loc = hits[d.ip].get("LOCATION", "")
    if not loc:
        return r.failed("no LOCATION header")
    import urllib.request
    try:
        with urllib.request.urlopen(loc, timeout=4) as f:
            json.loads(f.read())
    except Exception as e:
        return r.failed(f"LOCATION {loc} did not fetch: {e}")
    return r.passed(loc)


@test("C-2", "SSDP identity headers")
def c02(d, ctx):
    r = Res("C-2", c02._name)
    h = ctx.get("ssdp")
    if not h:
        return r.skipped("C-1 did not run")
    want = {"X-MRROIP-ID": "device_id", "X-MRROIP-NAME": "device_name",
            "X-MRROIP-TYPE": "device_type", "X-MRROIP-CLASS": "device_class"}
    bad = [f"{k}={h.get(k)!r} != {d.dfn.get(v)!r}"
           for k, v in want.items() if h.get(k) != d.dfn.get(v)]
    return r.failed("; ".join(bad)) if bad \
        else r.passed("a master can build a device list from the announcement alone")


@test("C-3", "mDNS name resolves")
def c03(d, ctx):
    r = Res("C-3", c03._name)
    name = d.dfn["device_name"]
    try:
        ip = socket.gethostbyname(f"{name}.local")
    except Exception as e:
        return r.failed(f"{name}.local did not resolve ({e}); "
                        "on Linux this needs avahi/nss-mdns installed on the host")
    return r.passed(f"-> {ip}") if ip == d.ip else r.failed(f"-> {ip}, expected {d.ip}")


@test("C-4", "whois probe")
def c04(d, ctx):
    r = Res("C-4", c04._name)
    hits = whois(d.ip)
    if d.ip not in hits:
        return r.failed("no reply on UDP 8266")
    got = hits[d.ip].get("id")
    return r.passed(got) if got == d.dfn["device_id"] \
        else r.failed(f"whois id {got} != {d.dfn['device_id']}")


@test("C-5", "identity survives a rename")
def c05(d, ctx):
    r = Res("C-5", c05._name)
    did, original = d.dfn["device_id"], d.dfn["device_name"]
    if len(did.split(":")) != 6:
        return r.failed(f"device_id {did!r} is not a MAC address")
    code, _ = d.set_config(device_name="probe-rename", persist=False)
    if code != 200:
        return r.failed(f"rename rejected ({code})")
    time.sleep(1.0)
    after = d.definition()["device_id"]
    d.set_config(device_name=original, persist=False)
    d.definition()
    return r.passed(did) if after == did \
        else r.failed(f"device_id changed on rename: {did} -> {after}")


# ---------------------------------------------------------------- definition

@test("C-6", "/definition shape")
def c06(d, ctx):
    r = Res("C-6", c06._name)
    dfn = d.dfn
    missing = [k for k in REQUIRED_TOP if k not in dfn]
    if missing:
        return r.failed(f"missing: {missing}")
    bad = []
    for p in dfn["parameters"]:
        n = p.get("name", "?")
        if "type" not in p or "default" not in p:
            bad.append(f"{n}(incomplete)"); continue
        if p["type"] in ("int", "float") and not ("min" in p and "max" in p):
            bad.append(f"{n}(no range)")
        if p["type"] == "string" and "max_len" not in p:
            bad.append(f"{n}(no max_len)")
    for k in CORE_PARAMS:
        if not d.param(k):
            bad.append(f"{k}(core parameter absent)")
    return r.failed(", ".join(bad)) if bad \
        else r.passed(f"{len(dfn['parameters'])} parameters")


@test("C-7", "device class declared")
def c07(d, ctx):
    r = Res("C-7", c07._name)
    cls = d.dfn.get("device_class")
    if cls not in CLASSES:
        return r.failed(f"device_class {cls!r} is not one of {CLASSES}")
    if not d.dfn.get("device_type") or not d.dfn.get("profile_version"):
        return r.failed("device_type or profile_version missing")
    return r.passed(f"{d.dfn['device_type']} {d.dfn['profile_version']}, {cls}")


@test("C-8", "core modes declared")
def c08(d, ctx):
    r = Res("C-8", c08._name)
    got = d.dfn.get("capabilities", {}).get("core_modes")
    if got is None:
        return r.failed("capabilities.core_modes absent")
    if sorted(got) != sorted(CORE_MODES):
        return r.failed(f"core_modes is {got}, must be exactly {CORE_MODES}")
    overlap = set(got) & set(d.modes)
    return r.failed(f"profile re-declares core modes {sorted(overlap)}") if overlap \
        else r.passed(f"profile modes: {d.modes}")


@test("C-9", "definition and config agree")
def c09(d, ctx):
    r = Res("C-9", c09._name)
    dk = {p["name"] for p in d.dfn["parameters"]}
    ck = set(d.cfg())
    if dk == ck:
        return r.passed(f"{len(dk)} keys")
    return r.failed(f"only in /definition: {sorted(dk - ck)}; "
                    f"only in /config: {sorted(ck - dk)}")


# ---------------------------------------------------------------- config

@test("C-10", "config applies immediately")
def c10(d, ctx):
    r = Res("C-10", c10._name)
    p = d.param("control_timeout_ms")
    cur = d.cfg()["control_timeout_ms"]
    new = 3000 if cur != 3000 else 4000
    code, _ = d.set_config(control_timeout_ms=new, persist=False)
    if code != 200:
        return r.failed(f"POST returned {code}")
    got = d.get_config()
    if got["config"]["control_timeout_ms"] != new:
        return r.failed("value did not take effect in RAM")
    m = got.get("_meta", {})
    if not m.get("dirty"):
        return r.failed("_meta.dirty not set after a non-persisted write")
    if "control_timeout_ms" not in m.get("dirty_keys", []):
        return r.failed("_meta.dirty_keys does not name the written key")
    d.set_config(control_timeout_ms=cur, persist=False)
    return r.passed(f"{cur} -> {new}, dirty flagged")


@test("C-11", "config volatility across reboot", star=True)
def c11(d, ctx):
    r = Res("C-11", c11._name, )
    if ctx["args"].no_prompt:
        return r.skipped("needs a power cycle (--no-prompt)")
    base = d.cfg()["control_timeout_ms"]
    volatile, durable = 2500, 3500

    d.set_config(control_timeout_ms=volatile, persist=False)
    if ask(f"wrote control_timeout_ms={volatile} WITHOUT persist — power-cycle now") == "s":
        return r.skipped("operator skipped")
    if not wait_back(d):
        return r.failed("device did not come back")
    if d.cfg()["control_timeout_ms"] == volatile:
        return r.failed("a non-persisted value survived the reboot — persist:true "
                        "means nothing and the whole commit model is absent")

    d.set_config(control_timeout_ms=durable, persist=True)
    if ask(f"wrote control_timeout_ms={durable} WITH persist — power-cycle again") == "s":
        return r.skipped("operator skipped half-way")
    if not wait_back(d):
        return r.failed("device did not come back")
    after = d.cfg()["control_timeout_ms"]
    d.set_config(control_timeout_ms=base, persist=True)
    return r.passed("volatile discarded, persisted kept") if after == durable \
        else r.failed(f"persisted value lost: {after}")


@test("C-12", "config write is atomic")
def c12(d, ctx):
    r = Res("C-12", c12._name)
    before = d.cfg()
    good = 5000 if before["control_timeout_ms"] != 5000 else 6000
    code, body = d.set_config(control_timeout_ms=good, udp_port=1)   # 1 is below min
    if code != 400:
        return r.failed(f"expected 400, got {code}")
    if body.get("applied") is not False:
        return r.failed('response does not state "applied": false')
    keys = {x.get("key") for x in body.get("details", [])}
    if "udp_port" not in keys:
        return r.failed(f"offending key not reported; details={body.get('details')}")
    if d.cfg()["control_timeout_ms"] != before["control_timeout_ms"]:
        return r.failed("the valid key was applied despite the rejection — not atomic")
    return r.passed("nothing applied, offender named")


@test("C-13", "unknown key rejected")
def c13(d, ctx):
    r = Res("C-13", c13._name)
    code, body = d.set_config(contrl_timeout_ms=5000)
    if code != 400:
        return r.failed(f"unknown key accepted or wrong status ({code})")
    reasons = {x.get("reason") for x in body.get("details", [])}
    return r.passed() if "unknown_key" in reasons \
        else r.failed(f"reason not 'unknown_key': {body.get('details')}")


@test("C-14", "range checking")
def c14(d, ctx):
    r = Res("C-14", c14._name)
    p = d.param("control_timeout_ms")
    lo = d.set_config(control_timeout_ms=p["min"] - 1)[0]
    hi = d.set_config(control_timeout_ms=p["max"] + 1)[0]
    if lo != 400: return r.failed(f"below min accepted ({lo})")
    if hi != 400: return r.failed(f"above max accepted ({hi})")
    at = d.set_config(control_timeout_ms=p["min"])[0]
    d.set_config(control_timeout_ms=p["default"], persist=False)
    return r.passed("bounds inclusive") if at == 200 \
        else r.failed(f"the minimum itself was rejected ({at}) — bounds are exclusive")


@test("C-15", "optimistic concurrency")
def c15(d, ctx):
    r = Res("C-15", c15._name)
    m = d.meta()
    if "config_version" not in m:
        return r.failed("no _meta.config_version")
    code, body = d.set_config(control_timeout_ms=2000, if_version=m["config_version"] - 1)
    if code != 409:
        return r.failed(f"stale if_version accepted ({code})")
    return r.passed() if "config" in body \
        else r.failed("409 did not return the current config")


# ---------------------------------------------------------------- control

@test("C-16", "control is idempotent", star=True)
def c16(d, ctx):
    r = Res("C-16", c16._name)
    h = ctx["hooks"]
    if not (h.activate and h.counter):
        return r.skipped("no profile hooks; a core suite cannot invent a mode")
    d.quiesce(); h.prepare_fast(d)
    base = _count(d, h)
    if base is None:
        return r.failed(f"state.profile.{h.counter} absent")
    for _ in range(10):
        d.control_udp(mode=h.activate)
        time.sleep(0.3)
    if not d.state().get("busy"):
        return r.failed(f"ten '{h.activate}' messages did not make the device busy{_fault_note(d)}")
    time.sleep(h.cycle_seconds(d) + 1.0)
    started = _count(d, h) - base
    d.control(mode=h.rest); d.wait_idle()
    if started > 1:
        return r.failed(f"{started} activities from one repeated desired state — the "
                        "message is being treated as an event, not a state")
    return r.passed(f"{h.counter} +{started}")


def _count(d, h):
    return d.pstate().get(h.counter)


def _fault_note(d):
    """A reported fault turns a puzzling FAIL into a diagnosis."""
    fault = d.state().get("fault")
    return f" — the device reports fault {fault!r}" if fault else ""


@test("C-17", "seq echoed on both transports")
def c17(d, ctx):
    r = Res("C-17", c17._name)
    s = 424242
    a = d.control_udp(seq=s, mode="hold")
    b = d.control(seq=s + 1, mode="hold")
    d.seq = s + 2
    if a.get("ack_seq") != s:
        return r.failed(f"UDP ack_seq {a.get('ack_seq')} != {s}")
    if b.get("ack_seq") != s + 1:
        return r.failed(f"HTTP ack_seq {b.get('ack_seq')} != {s+1}")
    return r.passed()


@test("C-18", "replay rejected, master restart tolerated")
def c18(d, ctx):
    r = Res("C-18", c18._name)
    raw = json.dumps({"seq": 900000, "mode": "hold"}).encode()
    first, second = d.control_udp(raw=raw), d.control_udp(raw=raw)
    if first.get("accepted") is not True:
        return r.failed("first datagram not accepted")
    if second.get("accepted") is not False or second.get("error") != "stale_seq":
        return r.failed("a replayed datagram was accepted — no duplicate detection")
    time.sleep(5.5)
    restart = d.control_udp(raw=json.dumps({"seq": 5, "mode": "hold"}).encode())
    d.seq = 900001
    return r.passed("replay blocked, restart re-anchored") \
        if restart.get("accepted") is True \
        else r.failed("a master that restarted its counter stayed locked out")


@test("C-19", "HTTP and UDP behave identically")
def c19(d, ctx):
    r = Res("C-19", c19._name)
    d.quiesce()
    body = {"seq": 700001, "mode": "hold"}
    u = d.control_udp(raw=json.dumps(body).encode())
    body["seq"] = 700002
    h = d.req("/control", body)[1]
    su, sh = u.get("state", {}), h.get("state", {})
    keys = (set(su) | set(sh)) - {"uptime_ms", "profile"}
    diff = [k for k in keys if su.get(k) != sh.get(k)]
    pu, ph = su.get("profile", {}), sh.get("profile", {})
    pk = (set(pu) | set(ph)) - {"position", "run_count", "last_run_ms", "elapsed_ms"}
    diff += [f"profile.{k}" for k in pk if pu.get(k) != ph.get(k)]
    return r.passed(f"{len(keys) + len(pk)} fields agree") if not diff \
        else r.failed(f"transports disagree on {diff} — two parsers, not one")


@test("C-20", "unknown mode rejected")
def c20(d, ctx):
    r = Res("C-20", c20._name)
    b = d.control(mode="zzz_not_a_mode")
    if b.get("accepted") is not False or b.get("error") != "unknown_mode":
        return r.failed(f"accepted={b.get('accepted')} error={b.get('error')}")
    u = d.control_udp(mode="zzz_not_a_mode")
    return r.passed() if u.get("error") == "unknown_mode" \
        else r.failed(f"UDP reported {u.get('error')!r}, HTTP reported 'unknown_mode'")


# ---------------------------------------------------------------- authority

@test("C-21", "timeout behaviour matches device_class", star=True)
def c21(d, ctx):
    r = Res("C-21", c21._name)
    h, cls = ctx["hooks"], d.device_class
    if not h.activate:
        return r.skipped("no profile hooks")
    d.quiesce(); h.prepare_fast(d)
    d.set_config(control_timeout_ms=2000, persist=False)
    d.control(mode=h.activate)
    if not d.wait(lambda s: s.get("busy"), 15):
        return r.failed(f"'{h.activate}' did not make the device busy{_fault_note(d)}")
    time.sleep(4.0)                                  # twice the timeout, silent
    st = d.state()
    auth, busy = st.get("authority"), st.get("busy")
    d.control(mode=h.rest); d.wait_idle()

    if cls == "mobile":
        return r.passed("stopped, as mobile requires") if not busy and auth == "idle" \
            else r.failed(f"mobile endpoint did not stop (busy={busy}, authority={auth})")
    if cls == "stationary":
        if auth != "autonomous":
            return r.failed(f"authority is {auth!r} after the timeout, expected "
                            "'autonomous' for a stationary endpoint with a programme")
        if not busy:
            return r.failed("the endpoint STOPPED when the master went quiet — "
                            "the timeout was applied to autonomous motion, which is the "
                            "literal reading of the draft and makes the product useless")
        return r.passed("authority released, autonomous motion continued")
    return r.passed(f"passive, authority={auth}") if auth == "idle" \
        else r.failed(f"passive endpoint reports authority={auth!r}")


@test("C-22", "unattended operation", star=True)
def c22(d, ctx):
    r = Res("C-22", c22._name)
    h, cls = ctx["hooks"], d.device_class
    if cls != "stationary":
        return r.skipped(f"only meaningful for stationary endpoints (this is {cls})")
    if not (h.activate and h.counter):
        return r.skipped("no profile hooks")
    d.quiesce(); h.prepare_fast(d)
    if not d.cfg().get("autonomous_on_boot", True):
        d.set_config(autonomous_on_boot=True, persist=False)
    d.control(mode=h.activate)
    d.control(mode="release")
    base, cyc = _count(d, h), h.cycle_seconds(d)
    t0 = time.monotonic()
    while time.monotonic() - t0 < cyc * 4:
        time.sleep(2.0)
        if not ctx["args"].quiet:
            print(".", end="", flush=True)
    done = _count(d, h) - base
    d.control(mode=h.rest); d.wait_idle()
    return r.passed(f"{done} activities unattended") if done >= 3 \
        else r.failed(f"only {done} in {cyc*4:.0f} s with no master — autonomy stalls")


@test("C-23", "release is immediate")
def c23(d, ctx):
    r = Res("C-23", c23._name)
    d.set_config(control_timeout_ms=10000, persist=False)
    d.control(mode="hold")
    if d.state().get("authority") != "commanded":
        return r.failed("a control message did not assert authority")
    d.control(mode="release")
    time.sleep(0.4)                                  # far inside control_timeout_ms
    a = d.state().get("authority")
    d.set_config(control_timeout_ms=2000, persist=False)
    return r.passed(a) if a in ("autonomous", "idle") \
        else r.failed(f"authority still {a!r} — release waits for the timeout")


@test("C-24", "estop stops, latches, survives a timeout")
def c24(d, ctx):
    r = Res("C-24", c24._name)
    h = ctx["hooks"]
    d.quiesce(); h.prepare_fast(d)
    if h.activate:
        d.control(mode=h.activate)
        if not d.wait(lambda s: s.get("busy"), 15):
            return r.failed(f"'{h.activate}' did not make the device busy{_fault_note(d)}")
    t0 = time.monotonic()
    d.control_udp(mode="estop")
    st = d.wait(lambda s: not s.get("busy"), 2.0, poll=0.05)
    dt = (time.monotonic() - t0) * 1000
    if st is None:
        return r.failed("estop did not take effect within 2 s")
    if dt > 300:
        return r.failed(f"estop took {dt:.0f} ms")
    if h.activate:
        b = d.control(mode=h.activate)
        if b.get("accepted") is not False or b.get("error") != "latched_estop":
            d.control(mode="reset")
            return r.failed("a command was accepted while estop was latched")
    time.sleep(4.0)                                  # outlive the control timeout
    still = d.state()
    if still.get("busy") or still.get("mode") == h.activate:
        d.control(mode="reset")
        return r.failed("estop cleared itself when the master went quiet — a safety "
                        "stop must survive loss of the master")
    d.control(mode="reset")
    ok = d.state().get("fault") is None and not d.state().get("busy")
    return r.passed(f"latched, cleared by reset ({dt:.0f} ms to stop)") if ok \
        else r.failed("reset did not clear the latch")


@test("C-25", "/state polling does not hold authority")
def c25(d, ctx):
    r = Res("C-25", c25._name)
    d.quiesce()
    d.set_config(control_timeout_ms=2000, persist=False)
    d.control(mode="hold")
    t0, released = time.monotonic(), None
    while time.monotonic() - t0 < 8:
        if d.state().get("authority") != "commanded":
            released = time.monotonic() - t0
            break
        time.sleep(0.2)                              # 5 Hz, and no control messages
    if released is None:
        return r.failed("authority never expired while /state was polled — polling is "
                        "being counted as a control message")
    return r.passed(f"expired after {released:.1f} s despite polling") if released < 5 \
        else r.failed(f"expiry delayed to {released:.1f} s by polling")


@test("C-26", "authority transfer is visible")
def c26(d, ctx):
    r = Res("C-26", c26._name)
    if not ctx["args"].second_host:
        return r.skipped("run with --second-host <ip of another machine> to exercise "
                         "authority_taken_from")
    return r.skipped("two-host mode not automated; command from the second host while "
                     "this one holds authority and check for authority_taken_from")


@test("C-27", "error strings match across transports")
def c27(d, ctx):
    r = Res("C-27", c27._name)
    cases = [({"mode": "zzz"}, "unknown_mode"),
             ({}, None)]                             # missing mode: any error, but same
    bad = []
    for body, _ in cases:
        b = dict(body); b["seq"] = d.seq + 1
        h = d.req("/control", b)[1].get("error")
        b["seq"] = d.seq + 2
        u = d.control_udp(raw=json.dumps(b).encode()).get("error")
        d.seq += 3
        if h != u:
            bad.append(f"{body} -> HTTP {h!r} vs UDP {u!r}")
    return r.failed("; ".join(bad)) if bad else r.passed("same vocabulary on both")


@test("C-28", "announcement rate-limit recovery")
def c28(d, ctx):
    r = Res("C-28", c28._name)
    if not ctx["args"].slow:
        return r.skipped("pass --slow, or build with a shortened recovery window")
    base = d.cfg()["announce_interval_s"]
    d.set_config(announce_interval_s=0, persist=False)
    time.sleep(2)
    if ssdp_search(timeout=6.0).get(d.ip):
        d.set_config(announce_interval_s=base, persist=False)
        return r.failed("still announcing after announce_interval_s=0")
    print("    waiting out the recovery window (leave the device alone) ...")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1900:
        if ssdp_search(timeout=10.0).get(d.ip):
            d.set_config(announce_interval_s=base, persist=False)
            return r.passed(f"resumed after {(time.monotonic()-t0)/60:.1f} min")
        time.sleep(30)
    d.set_config(announce_interval_s=base, persist=False)
    return r.failed("announcements never resumed — the device can be lost permanently")


@test("C-29", "malformed input survives")
def c29(d, ctx):
    r = Res("C-29", c29._name)
    probes = [b"", b"{", b'{"seq":1,"mode":', b"\x00\xff\xfe\x01" * 64,
              b'{"seq":1,"mode":"hold"' + b" " * 5000 + b"}",
              json.dumps({"seq": 1, "mode": 42}).encode(),
              json.dumps({"mode": "hold"}).encode()]          # no seq
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1.5)
    try:
        for p in probes:
            s.sendto(p, (d.ip, d.udp_port))
            try:    s.recvfrom(4096)
            except socket.timeout: pass
    finally:
        s.close()
    code, _ = d.req("/control", {"seq": 1, "mode": "hold", "pad": "x" * 8192})
    if code not in (400, 413):
        return r.failed(f"8 kB body returned {code}, expected 413 or 400")
    if d.req("/no_such_path")[0] != 404:
        return r.failed("unknown path did not return 404")
    time.sleep(0.5)
    try:
        d.state()
    except Exception as e:
        return r.failed(f"unresponsive after malformed input: {e}")
    return r.passed(f"{len(probes)} malformed datagrams + oversized body, still up")


@test("C-30", "AP-mode parity")
def c30(d, ctx):
    return Res("C-30", c30._name).skipped(
        "erase credentials, join the fallback AP, re-run with "
        "--host 192.168.4.1 --only C-1,C-6,C-10,C-16")


@test("C-31", "30 minute soak")
def c31(d, ctx):
    r = Res("C-31", c31._name)
    if not ctx["args"].soak:
        return r.skipped("pass --soak to run (30 minutes)")
    h = ctx["hooks"]
    d.quiesce(); h.prepare_fast(d)
    if h.activate:
        d.control(mode=h.activate); d.control(mode="release")
    heap, t0 = [], time.monotonic()
    while time.monotonic() - t0 < 1800:
        try:
            st = d.state()
        except Exception as e:
            return r.failed(f"stopped responding after "
                            f"{(time.monotonic()-t0)/60:.1f} min: {e}")
        if st.get("uptime_ms", 1e12) < (time.monotonic() - t0) * 900:
            return r.failed("uptime went backwards — the device rebooted")
        if "free_heap" in st:
            heap.append((time.monotonic() - t0, st["free_heap"]))
        time.sleep(0.2)
    if h.activate:
        d.control(mode=h.rest)
    tail = [v for t, v in heap if t > 600]
    if len(tail) > 50:
        drift = tail[0] - tail[-1]
        return r.failed(f"free heap fell {drift} B over the last 20 min — leak") \
            if drift > 4096 else r.passed(f"30 min, heap drift {drift} B")
    return r.passed("30 min, no reboot (expose free_heap in /state for leak detection)")


CORE_TESTS = [c01, c02, c03, c04, c05, c06, c07, c08, c09, c10, c11, c12, c13, c14,
              c15, c16, c17, c18, c19, c20, c21, c22, c23, c24, c25, c26, c27, c28,
              c29, c30, c31]


# ---------------------------------------------------------------- driver

def load_profile(name):
    """Import profile_<name>.py. Returns (tests, hooks) or (None, Hooks())."""
    if not name:
        return [], Hooks()
    try:
        mod = importlib.import_module(f"profile_{name}")
    except ImportError:
        print(f"  note: no profile_{name}.py alongside this script — running the core "
              f"suite only.\n        Core tests that need a device to do something "
              f"will skip.")
        return [], Hooks()
    return list(getattr(mod, "TESTS", [])), getattr(mod, "HOOKS", Hooks())()


def run(tests, d, ctx, args, results):
    only = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else set()
    for fn in tests:
        tid = fn._id
        if only and tid not in only: continue
        if tid in skip:
            results.append(Res(tid, fn._name).skipped("--skip")); continue
        if not args.quiet:
            print(f"[{tid:5s}] {fn._name} ", end="", flush=True)
        try:
            res = fn(d, ctx)
        except Exception as e:
            res = Res(tid, fn._name).failed(f"exception: {type(e).__name__}: {e}")
        res.star = getattr(fn, "_star", False)
        results.append(res)
        if not args.quiet:
            print({True: "ok", False: "FAIL", None: "skip"}[res.ok]
                  + (f" — {res.note}" if res.note else ""))
        if tid == "C-6" and res.ok is False:
            print("\n/definition is broken; everything after it is meaningless. Stopping.")
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host")
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--udp-port", type=int, default=5300)
    ap.add_argument("--profile", help="override the profile module; default is "
                                      "device_type from /definition")
    ap.add_argument("--core-only", action="store_true")
    ap.add_argument("--only", help="comma-separated test ids, e.g. C-11,P-15")
    ap.add_argument("--skip", help="comma-separated test ids")
    ap.add_argument("--soak", action="store_true", help="run C-31 (30 min)")
    ap.add_argument("--slow", action="store_true", help="run C-28 (up to 32 min)")
    ap.add_argument("--second-host", help="address of a second machine, for C-26")
    ap.add_argument("--no-prompt", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    ip = args.host
    if args.discover or not ip:
        print("SSDP M-SEARCH ...")
        hits = ssdp_search()
        if not hits:
            print("  nothing on SSDP; trying the whois probe")
            hits = whois()
        if not hits:
            print("No endpoint found. If the device is up, multicast is most likely "
                  "being blocked\nbetween this host and it — a guest network or a mesh "
                  "AP will do that silently.")
            return 2
        for k, v in hits.items():
            print(f"  {k}  {v.get('X-MRROIP-NAME') or v.get('name') or '?'}  "
                  f"{v.get('X-MRROIP-TYPE') or v.get('type') or ''}")
        ip = sorted(hits)[0]
        print(f"using {ip}\n")

    d = Dev(ip, args.udp_port)
    try:
        dfn = d.definition()
    except Exception as e:
        print(f"GET /definition failed: {e}")
        return 2
    print(f"{dfn.get('proto')} {dfn.get('proto_version')}   {dfn.get('device_name')}   "
          f"{dfn.get('device_id')}\nprofile {dfn.get('device_type')} "
          f"{dfn.get('profile_version')}   class {dfn.get('device_class')}   "
          f"fw {dfn.get('firmware')}\n")
    d.udp_port = dfn.get("endpoints", {}).get("udp_control_port", args.udp_port)

    ptests, hooks = load_profile(args.profile or dfn.get("device_type"))
    ctx = {"args": args, "hooks": hooks}
    results = []

    print("── core suite ─────────────────────────────────────────────────────────")
    ok = run(CORE_TESTS, d, ctx, args, results)
    if ok and ptests and not args.core_only:
        print("\n── profile suite: " + dfn.get("device_type", "?")
              + " " + "─" * max(0, 52 - len(dfn.get("device_type", "?"))))
        run(ptests, d, ctx, args, results)

    try:
        d.quiesce()
    except Exception:
        pass

    print("\n" + "=" * 78)
    npass = nfail = nskip = 0
    for r in results:
        mark = {True: " ok ", False: "FAIL", None: "skip"}[r.ok]
        print(f"{'*' if getattr(r, 'star', False) else ' '} {r.id:5s} {mark}  "
              f"{r.name:<44s} {r.note[:52]}")
        npass += r.ok is True; nfail += r.ok is False; nskip += r.ok is None
    print("=" * 78)
    print(f"{npass} passed, {nfail} failed, {nskip} skipped   "
          "(* = the contested decisions of core §17)")
    starred = [r for r in results if getattr(r, "star", False) and r.ok is False]
    if starred:
        print("\nThe starred failures are the point of the exercise. Each is a place "
              "where the\nworking draft is ambiguous and this implementation chose "
              "wrongly, or not at all:")
        for r in starred:
            print(f"  {r.id}  {r.name} — {r.note}")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
