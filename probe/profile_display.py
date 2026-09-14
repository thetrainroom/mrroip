"""
profile_display.py — profile tests and core-suite hooks for the `display` profile: a bitmap-only SSD1306
endpoint (../../oled/MMROIP-PLAN.md §2). mmroip_probe.py loads it when /definition.device_type is "display".

    python3.12 mmroip_probe.py --host 192.168.10.164 --no-prompt --only C-16,C-21,C-24,P-1,P-2,P-3,P-4,P-5,P-6

P-6 changes `panel` with persist, so the device restarts twice; it restores the original panel.
P-8 … P-11 test partial updates (rectangles on top of the image on screen, guarded by base_crc32).
"""

import time
import zlib

from mmroip import image
from mmroip_lib import Hooks, Res, test, wait_back


class DisplayHooks(Hooks):
    #: blinking is the display's only activity: busy while it runs
    activate = "blink"
    rest = "show"
    #: counts transitions into blinking, so a repeated "blink" must add exactly one
    counter = "blink_starts"

    def cycle_seconds(self, d):
        return 1.0      # blinking never ends by itself; a second shows it running

    def prepare_fast(self, d):
        d.set_config(blink_period_ms=100, persist=False)

    def provoke_fault(self, d):
        return False    # display_not_found needs the panel disconnected at boot


HOOKS = DisplayHooks


def _size(d):
    return image.image_size(d.dfn or d.definition())


def _crc_on_screen(d):
    return (d.pstate().get("image") or {}).get("crc32")


def _reasons(reply):
    return {x.get("reason") for x in reply.get("details", [])}


def _fault(d):
    return d.state().get("fault")


@test("P-1", "image round trip")
def p01(d, ctx):
    r = Res("P-1", p01._name)
    d.quiesce()
    if _fault(d):
        return r.skipped(f"the device reports fault {_fault(d)!r}: nothing can be shown (see P-7)")
    w, h = _size(d)
    data = image.pattern("checker", w, h)
    reply = d.control(mode="show", objects={"image": image.encode(data)})
    if not reply.get("accepted"):
        return r.failed(f"rejected: {reply.get('error')} {reply.get('details')}")
    want, got = "%08x" % zlib.crc32(data), _crc_on_screen(d)
    return r.passed(f"{w}x{h}, crc32 {got}") if got == want \
        else r.failed(f"/state reports crc32 {got}, sent {want}")


@test("P-2", "image of the wrong size rejected")
def p02(d, ctx):
    r = Res("P-2", p02._name)
    w, h = _size(d)
    before = _crc_on_screen(d)
    short = image.encode(image.pattern("border", w, h)[:-8])
    reply = d.control(mode="show", objects={"image": short})
    if reply.get("accepted") is not False or reply.get("error") != "invalid_object_state":
        return r.failed(f"accepted={reply.get('accepted')} error={reply.get('error')}")
    if "wrong_size" not in _reasons(reply):
        return r.failed(f"details do not say wrong_size: {reply.get('details')}")
    return r.passed("rejected, picture kept") if _crc_on_screen(d) == before \
        else r.failed("a rejected image replaced the one on screen")


@test("P-3", "invalid Base64 rejected")
def p03(d, ctx):
    r = Res("P-3", p03._name)
    w, h = _size(d)
    before = _crc_on_screen(d)
    garbage = "!" * len(image.encode(image.pattern("border", w, h)))     # right length, not Base64
    reply = d.control(mode="show", objects={"image": garbage})
    if reply.get("accepted") is not False or "invalid_base64" not in _reasons(reply):
        return r.failed(f"accepted={reply.get('accepted')} details={reply.get('details')}")
    return r.passed("rejected, picture kept") if _crc_on_screen(d) == before \
        else r.failed("a rejected image replaced the one on screen")


@test("P-4", "screen off blanks, on restores")
def p04(d, ctx):
    r = Res("P-4", p04._name)
    if _fault(d):
        return r.skipped(f"the device reports fault {_fault(d)!r} (see P-7)")
    off = d.control(mode="show", objects={"screen": "off"})
    p = off.get("state", {}).get("profile", {})
    if not off.get("accepted") or p.get("screen") != "off" or p.get("phase") != "blank":
        return r.failed(f"off: accepted={off.get('accepted')} screen={p.get('screen')} phase={p.get('phase')}")
    on = d.control(mode="show", objects={"screen": "on"})
    p = on.get("state", {}).get("profile", {})
    if not on.get("accepted") or p.get("screen") != "on" or p.get("phase") == "blank":
        return r.failed(f"on: accepted={on.get('accepted')} screen={p.get('screen')} phase={p.get('phase')}")
    return r.passed(f"off -> blank, on -> {p.get('phase')}")


@test("P-5", "contrast applies and is range-checked")
def p05(d, ctx):
    r = Res("P-5", p05._name)
    current = d.cfg()["contrast"]
    new = 60 if current != 60 else 90
    code, body = d.set_config(contrast=new, persist=False)
    if code != 200 or d.cfg()["contrast"] != new:
        d.set_config(contrast=current, persist=False)
        return r.failed(f"write returned {code}, contrast now {d.cfg().get('contrast')}")
    too_high = d.set_config(contrast=256)[0]
    d.set_config(contrast=current, persist=False)
    if too_high != 400:
        return r.failed(f"contrast=256 returned {too_high}")
    return r.passed(f"{current} -> {new} -> {current}; brightness itself is not measurable here")


@test("P-6", "panel applies at restart")
def p06(d, ctx):
    r = Res("P-6", p06._name)
    p = d.param("panel")
    if not p or p.get("applies") != "restart" or not p.get("values"):
        return r.failed("panel is not declared with values and applies: restart")
    current = d.cfg()["panel"]
    other = next(v for v in p["values"] if v != current)

    code, body = d.set_config(panel=other)
    if code != 400 or "requires_persist" not in _reasons(body):
        return r.failed(f"a RAM-only panel write returned {code} {body.get('details')}")

    def switch(panel):
        code, _ = d.set_config(panel=panel, persist=True)
        if code != 200:
            return None
        time.sleep(2.0)                     # it answers first, then restarts
        return image.image_size(d.definition()) if wait_back(d) else None

    size = switch(other)
    restored = switch(current)
    want = tuple(int(x) for x in other.split("x"))
    if size != want:
        return r.failed(f"after panel={other} and a restart, /definition reports {size}")
    if restored != tuple(int(x) for x in current.split("x")):
        return r.failed(f"could not restore panel {current}: {restored}")
    return r.passed(f"{current} -> {other} {size} -> {current}")


@test("P-7", "a display fault is named and refuses pictures")
def p07(d, ctx):
    r = Res("P-7", p07._name)
    fault = _fault(d)
    if not fault:
        return r.skipped("no fault reported; run against a device without a working display")
    if fault not in ("display_not_found", "display_lost"):
        return r.failed(f"unknown fault {fault!r}")
    reply = d.control(mode="show")
    if reply.get("accepted") is not False or reply.get("error") != "latched_fault":
        return r.failed(f"show while faulty: accepted={reply.get('accepted')} error={reply.get('error')}")
    after = d.control(mode="reset")
    if not after.get("accepted"):
        return r.failed(f"reset refused: {after.get('error')}")
    still = after.get("state", {}).get("fault")
    return r.passed(f"{fault}: show refused with latched_fault; reset retried the display, fault now {still!r}")


RECT = (8, 8, 16, 8)                        # inside every panel, the smallest being 64x32
SOLID = bytes([0xFF]) * (2 * 8)             # all lit: never equal to the checker pattern underneath


def _show_checker(d):
    w, h = _size(d)
    data = image.pattern("checker", w, h)
    reply = d.control(mode="show", objects={"image": image.encode(data)})
    return data if reply.get("accepted") else None


@test("P-8", "partial update replaces its rectangle")
def p08(d, ctx):
    r = Res("P-8", p08._name)
    d.quiesce()
    if _fault(d):
        return r.skipped(f"the device reports fault {_fault(d)!r} (see P-7)")
    base = _show_checker(d)
    if base is None:
        return r.failed("the full image underneath was rejected")
    w, _ = _size(d)
    bus_before = (d.pstate().get("image") or {}).get("bus_bytes")
    reply = d.patch_image(image.crc32(base), [(*RECT, SOLID)])
    if not reply.get("accepted"):
        return r.failed(f"rejected: {reply.get('error')} {reply.get('details')}")
    want, got = image.crc32(image.paste(base, w, *RECT, SOLID)), _crc_on_screen(d)
    if got != want:
        return r.failed(f"/state reports crc32 {got}, the patched image is {want}")
    bus_after = (d.pstate().get("image") or {}).get("bus_bytes")
    bus = f", {bus_after - bus_before} bytes on the display bus" if None not in (bus_before, bus_after) else ""
    return r.passed(f"{RECT[2]}x{RECT[3]} at ({RECT[0]}, {RECT[1]}), crc32 {got}{bus}")


@test("P-9", "partial update on the wrong image refused")
def p09(d, ctx):
    r = Res("P-9", p09._name)
    d.quiesce()
    if not _fault(d) and _show_checker(d) is None:
        return r.failed("the full image underneath was rejected")
    before = _crc_on_screen(d)
    wrong = "00000000" if before != "00000000" else "11111111"
    reply = d.patch_image(wrong, [(*RECT, SOLID)])
    if reply.get("accepted") is not False or "stale_base" not in _reasons(reply):
        return r.failed(f"accepted={reply.get('accepted')} details={reply.get('details')}")
    return r.passed("stale_base, picture kept") if _crc_on_screen(d) == before \
        else r.failed("a refused partial update changed the picture")


@test("P-10", "a repeated partial update is harmless")
def p10(d, ctx):
    r = Res("P-10", p10._name)
    d.quiesce()
    if _fault(d):
        return r.skipped(f"the device reports fault {_fault(d)!r} (see P-7)")
    base = _show_checker(d)
    if base is None:
        return r.failed("the full image underneath was rejected")
    first = d.patch_image(image.crc32(base), [(*RECT, SOLID)])
    after = _crc_on_screen(d)
    again = d.patch_image(image.crc32(base), [(*RECT, SOLID)])     # its base is outdated now, its content is not
    if not first.get("accepted") or not again.get("accepted"):
        return r.failed(f"first accepted={first.get('accepted')}, repeat accepted={again.get('accepted')} "
                        f"{again.get('details')}")
    return r.passed("accepted again, picture unchanged") if _crc_on_screen(d) == after \
        else r.failed("the repeat changed the picture")


@test("P-11", "invalid rectangles refused")
def p11(d, ctx):
    r = Res("P-11", p11._name)
    d.quiesce()
    w, h = _size(d)
    before = _crc_on_screen(d)
    base = before or "00000000"
    cases = {
        "out_of_bounds": [(w - 4, 0, 8, 8, bytes(8))],
        "wrong_size": [(*RECT, SOLID[:-2])],
        "too_many_rects": [(*RECT, SOLID)] * 9,
        "invalid_base64": None,
        "missing_base_crc32": None,
    }
    wrong = []
    for reason, rects in cases.items():
        if reason == "invalid_base64":
            reply = d.control(mode="show", objects={"image": {"base_crc32": base, "rects": [
                {"x": 8, "y": 8, "w": 16, "h": 8, "data": "!" * len(image.encode(SOLID))}]}})
        elif reason == "missing_base_crc32":
            reply = d.control(mode="show", objects={"image": {"rects": [
                {"x": 8, "y": 8, "w": 16, "h": 8, "data": image.encode(SOLID)}]}})
        else:
            reply = d.patch_image(base, rects)
        if reply.get("accepted") is not False or reason not in _reasons(reply):
            wrong.append(f"{reason}: accepted={reply.get('accepted')} details={reply.get('details')}")
    if wrong:
        return r.failed("; ".join(wrong))
    return r.passed(f"{len(cases)} reasons, picture kept") if _crc_on_screen(d) == before \
        else r.failed("a refused partial update changed the picture")


TESTS = [p01, p02, p03, p04, p05, p06, p07, p08, p09, p10, p11]
