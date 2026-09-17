"""
profile_display.py — profile tests and core-suite hooks for the `display` profile. mmroip_probe.py loads it when
/definition.device_type is "display", for both kinds of panel:

  * 1-bit SSD1306 endpoints (../../oled/MMROIP-PLAN.md §2), format "1bpp-row-msb": P-1 … P-11
  * colour endpoints (question 16), format "rgb565be": P-12 … P-15, where pixels arrive with PUT /objects/image
    and the device remembers them as a CRC32 per tile

Each test skips when it does not fit the endpoint's format.

    python3.12 mmroip_probe.py --host 192.168.10.164 --no-prompt --only C-16,C-21,C-24,P-1,P-2,P-3,P-4,P-5,P-6

P-6 changes `panel` with persist, so the device restarts twice; it restores the original panel.
P-8 … P-11 test partial updates (rectangles on top of the image on screen, guarded by base_crc32).
"""

import time
import zlib

from mmroip import image, rtp
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


def _image_profile(d):
    dfn = d.dfn or d.definition()
    return next((o.get("profile", {}) for o in dfn.get("objects", []) if o.get("id") == "image"), {})


def _format(d):
    return _image_profile(d).get("format", "")


def _one_bit(d, r):
    """A reason to skip, when the endpoint is not a 1-bit panel."""
    return None if _format(d) == "1bpp-row-msb" else f"format is {_format(d)!r}, not a 1-bit panel"


def _colour(d, r):
    """A reason to skip, when the endpoint is not a colour panel that takes uploads."""
    return None if _format(d) == "rgb565be" else f"format is {_format(d)!r}, not a colour panel"


def _image_state(d):
    return d.pstate().get("image") or {}


@test("P-1", "image round trip")
def p01(d, ctx):
    r = Res("P-1", p01._name)
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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
    skip = _one_bit(d, r)
    if skip:
        return r.skipped(skip)
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


@test("P-12", "colour upload round trip")
def p12(d, ctx):
    r = Res("P-12", p12._name)
    skip = _colour(d, r)
    if skip:
        return r.skipped(skip)
    d.quiesce()
    w, h = _size(d)
    tile = d.image_tile_px()
    pixels = image.pattern_rgb565("bars", w, h)
    t0 = time.monotonic()
    reply = d.put_image(pixels)
    seconds = time.monotonic() - t0
    if not reply.get("accepted"):
        return r.failed(f"rejected: {reply.get('error')} {reply.get('details')}")
    want, got = image.image_id(pixels, w, h, tile), _image_state(d).get("id")
    if got != want:
        return r.failed(f"/state reports image id {got}, the host computes {want}")
    return r.passed(f"{len(pixels)} bytes in {seconds:.2f} s, id {got}, {tile} px tiles")


@test("P-13", "tile-aligned partial update")
def p13(d, ctx):
    r = Res("P-13", p13._name)
    skip = _colour(d, r)
    if skip:
        return r.skipped(skip)
    w, h = _size(d)
    tile = d.image_tile_px()
    base_picture = image.pattern_rgb565("gradient", w, h)
    if not d.put_image(base_picture).get("accepted"):
        return r.failed("the picture underneath was rejected")
    base_id = _image_state(d).get("id")

    rect = (tile, tile * 2, tile * 3, tile * 2)
    patch = image.pattern_rgb565("checker", w, h)
    mixed = bytearray(base_picture)
    stride = w * 2
    for row in range(rect[3]):
        at = (rect[1] + row) * stride + rect[0] * 2
        mixed[at: at + rect[2] * 2] = patch[at: at + rect[2] * 2]
    mixed = bytes(mixed)

    reply = d.put_image(image.crop_rgb565(mixed, w, *rect), *rect, base=base_id)
    if not reply.get("accepted"):
        return r.failed(f"rejected: {reply.get('error')} {reply.get('details')}")
    want, got = image.image_id(mixed, w, h, tile), _image_state(d).get("id")
    return r.passed(f"{rect[2]}x{rect[3]} at ({rect[0]}, {rect[1]}), id {got}") if got == want \
        else r.failed(f"/state reports {got}, the host computes {want}")


@test("P-14", "invalid uploads refused")
def p14(d, ctx):
    r = Res("P-14", p14._name)
    skip = _colour(d, r)
    if skip:
        return r.skipped(skip)
    w, h = _size(d)
    tile = d.image_tile_px()
    picture = image.pattern_rgb565("bars", w, h)
    if not d.put_image(picture).get("accepted"):
        return r.failed("the picture underneath was rejected")
    before = _image_state(d).get("id")
    rect = (tile, tile, tile * 2, tile * 2)
    pixels = image.crop_rgb565(picture, w, *rect)

    cases = {
        "stale_base": d.put_image(pixels, *rect, base="00000000"),
        "missing_base": d.put_image(pixels, *rect),
        "not_tile_aligned": d.put_image(pixels, rect[0] + 1, rect[1], rect[2], rect[3], base=before),
        "wrong_size": d.put_image(pixels[:-4], *rect, base=before),
        "upload_only": d.control(mode="show", objects={"image": "AAAA"}),
    }
    wrong = [f"{reason}: {reply.get('error')} {reply.get('details')}"
             for reason, reply in cases.items()
             if reply.get("accepted") is not False or reason not in _reasons(reply)]
    if wrong:
        return r.failed("; ".join(wrong))
    return r.passed(f"{len(cases)} reasons, picture kept") if _image_state(d).get("id") == before \
        else r.failed("a refused upload changed the picture")


@test("P-15", "streamed frames arrive whole")
def p15(d, ctx):
    r = Res("P-15", p15._name)
    skip = _colour(d, r)
    if skip:
        return r.skipped(skip)
    stream = next((o for o in (d.dfn or d.definition()).get("objects", []) if o.get("id") == "stream"), None)
    if not stream:
        return r.skipped("this endpoint has no stream object")
    port = stream.get("profile", {}).get("default_port", 5004)
    w, h = _size(d)
    tile = d.image_tile_px()

    if not d.control(mode="show", objects={"stream": {"port": port}}).get("accepted"):
        return r.failed("the stream did not start")
    before = d.pstate().get("stream", {})
    sender = rtp.Sender(d.ip, port, w, h, "rgb565be", fps=5)
    frames, last = 8, None
    for i in range(frames):
        last = image.pattern_rgb565("gradient" if i % 2 else "checker", w, h)
        sender.send_frame(last)
    sender.close()
    time.sleep(1.2)
    state = d.pstate()
    after = state.get("stream", {})
    d.control(mode="show", objects={"stream": "off"})

    received = after.get("frames", 0) - before.get("frames", 0)
    lost = after.get("lost_packets", 0) - before.get("lost_packets", 0)
    if received < frames:
        return r.failed(f"{received} of {frames} frames arrived, {lost} packets lost — the endpoint cannot "
                        f"keep up at 5 frames a second")
    want, got = image.image_id(last, w, h, tile), (state.get("image") or {}).get("id")
    return r.passed(f"{received} frames, {lost} packets lost, id {got}") if got == want \
        else r.failed(f"after {received} frames /state reports {got}, the host computes {want} "
                      f"(tiles known: {(state.get('image') or {}).get('tiles_known')})")


TESTS = [p01, p02, p03, p04, p05, p06, p07, p08, p09, p10, p11, p12, p13, p14, p15]
