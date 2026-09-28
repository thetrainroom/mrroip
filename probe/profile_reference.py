# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
profile_reference.py — profile tests and core-suite hooks for the `reference` profile
(../conformance/PROFILE-REFERENCE.md): a lamp that is on, off or blinking. Every core implementation in this
repository ships it, so the hook-driven core tests (C-16, C-21, C-22, C-24) run on each of them.

    python3.12 mrroip_probe.py --host 127.0.0.1:8080 --no-prompt

P-4 raises the simulated fault and clears it with reset.
"""

import time

from mrroip_lib import Hooks, Res, test


class ReferenceHooks(Hooks):
    activate = "blink"
    rest = "off"
    counter = "blink_starts"

    def cycle_seconds(self, d):
        cfg = d.cfg()
        return cfg.get("blink_period_ms", 500) * (cfg.get("programme_blinks", 4) + 1) / 1000.0

    def prepare_fast(self, d):
        d.set_config(blink_period_ms=100, programme_blinks=2, persist=False)

    def provoke_fault(self, d):
        d.set_config(simulate_fault=1, persist=False)
        return d.state().get("fault") is not None

    def clear_fault(self, d):
        d.set_config(simulate_fault=0, persist=False)
        d.control(mode="reset")


HOOKS = ReferenceHooks


def _reasons(reply):
    return {x.get("reason") for x in reply.get("details", [])}


@test("P-1", "modes reach the lamp")
def p01(d, ctx):
    r = Res.of("P-1")
    d.quiesce()
    on = d.control(mode="on", objects={"level": 40})
    lamp_on, level = d.pstate().get("lamp"), d.pstate().get("level")
    off = d.control(mode="off")
    lamp_off = d.pstate().get("lamp")
    if not (on.get("accepted") and off.get("accepted")):
        return r.failed(f"rejected: {on.get('error') or off.get('error')}")
    if (lamp_on, level, lamp_off) != ("on", 40, "off"):
        return r.failed(f"on -> lamp={lamp_on} level={level}; off -> lamp={lamp_off}")
    return r.passed("on at 40 %, then off")


@test("P-2", "out-of-range level refused, nothing applied")
def p02(d, ctx):
    r = Res.of("P-2")
    d.quiesce()
    d.control(mode="on", objects={"level": 30})
    reply = d.control(mode="on", objects={"level": 101})
    level = d.pstate().get("level")
    d.control(mode="off")
    if reply.get("accepted") is not False or reply.get("error") != "invalid_object_state":
        return r.failed(f"accepted={reply.get('accepted')} error={reply.get('error')}")
    if "out_of_range" not in _reasons(reply):
        return r.failed(f"details do not say out_of_range: {reply.get('details')}")
    return r.passed("refused, level kept") if level == 30 else r.failed(f"level became {level}")


@test("P-3", "unknown object refused")
def p03(d, ctx):
    r = Res.of("P-3")
    reply = d.control(mode="on", objects={"level": 50, "zzz": 1})
    d.control(mode="off")
    if reply.get("accepted") is not False or "unknown_object" not in _reasons(reply):
        return r.failed(f"accepted={reply.get('accepted')} details={reply.get('details')}")
    return r.passed("unknown_object")


@test("P-4", "simulated fault latches until reset")
def p04(d, ctx):
    r = Res.of("P-4")
    h = ctx["hooks"]
    d.quiesce()
    if not h.provoke_fault(d):
        return r.failed("simulate_fault=1 did not raise state.fault")
    d.set_config(simulate_fault=0, persist=False)       # the condition clears; the latch must not
    refused = d.control(mode="on")
    still = d.state().get("fault")
    h.clear_fault(d)
    after = d.state().get("fault")
    if refused.get("error") != "latched_fault":
        return r.failed(f"'on' during the fault gave {refused.get('error')!r}, expected latched_fault")
    if still is None:
        return r.failed("the fault cleared itself when the condition went away")
    return r.passed(f"{still!r}, cleared by reset") if after is None else r.failed(f"reset left fault {after!r}")


@test("P-5", "blink_starts counts transitions into blinking")
def p05(d, ctx):
    r = Res.of("P-5")
    d.quiesce()
    ctx["hooks"].prepare_fast(d)
    d.control(mode="on")            # commanded: a stationary endpoint's programme stops counting
    base = d.pstate().get("blink_starts", 0)
    for mode in ("blink", "blink", "blink", "on", "blink"):
        d.control(mode=mode)
        time.sleep(0.05)
    got = d.pstate().get("blink_starts", 0) - base
    d.control(mode="off")
    return r.passed("+2 for two entries") if got == 2 else r.failed(f"+{got}, expected +2")


TESTS = [p01, p02, p03, p04, p05]
