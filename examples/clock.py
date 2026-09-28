#!/usr/bin/env python3
"""
clock.py — an analog clock on one or more MRRoIP displays: hour marks, hour and minute hands, and a dot that
runs round the face for the seconds. Drawn here in plain Python (no Pillow needed).

    python3.12 clock.py 192.168.10.164                         # local time
    python3.12 clock.py 192.168.10.164 192.168.10.165 --udp    # several displays, over UDP
    python3.12 clock.py 192.168.10.164 --speed 4 --start 06:00 # model-railway fast clock: 4× from 06:00
    python3.12 clock.py --preview 64x32                        # print one frame in the terminal
    python3.12 clock.py 192.168.10.164 --full                  # whole images every time, to compare traffic

The face is as large as the panel's shorter side and centred. Once a second, when the seconds dot moves, only
the rectangles that changed are sent (whole images with --full, or to a display without partial updates).
Ctrl+C stops and prints the bytes sent; the last image stays on the screen.
"""

import argparse
import math
import sys
import time
from pathlib import Path

try:
    import mrroip
except ImportError:                     # not installed: use the package in this repository
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import mrroip


class Canvas:
    """A 1-bit image in the display profile's wire format: row-major, MSB = leftmost pixel."""

    def __init__(self, width, height):
        self.w, self.h = width, height
        self.stride = (width + 7) // 8
        self.buf = bytearray(self.stride * height)

    def set(self, x, y):
        x, y = math.floor(x + 0.5), math.floor(y + 0.5)     # round() would round .5 to even and leave gaps
        if 0 <= x < self.w and 0 <= y < self.h:
            self.buf[y * self.stride + x // 8] |= 0x80 >> (x % 8)

    def line(self, x0, y0, x1, y1, thickness=1):
        """A line from (x0, y0) to (x1, y1); thickness 2 adds a neighbouring line on one side."""
        n = max(1, math.ceil(2 * math.hypot(x1 - x0, y1 - y0)))
        length = math.hypot(x1 - x0, y1 - y0) or 1
        nx, ny = -(y1 - y0) / length, (x1 - x0) / length       # unit normal
        for i in range(n + 1):
            x, y = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n
            for k in range(thickness):
                self.set(x + nx * k, y + ny * k)

    def disc(self, cx, cy, r):
        for dy in range(-math.ceil(r), math.ceil(r) + 1):
            for dx in range(-math.ceil(r), math.ceil(r) + 1):
                if dx * dx + dy * dy <= r * r + 0.5:
                    self.set(cx + dx, cy + dy)

    def text(self):
        return "\n".join("".join("#" if self.buf[y * self.stride + x // 8] & (0x80 >> (x % 8)) else "."
                                 for x in range(self.w)) for y in range(self.h))


def render(width, height, hh, mm, ss):
    """One frame of the analog clock at hh:mm:ss."""
    c = Canvas(width, height)
    size = min(width, height)
    if size < 16:
        raise ValueError(f"{width}x{height} is too small for a clock face")
    cx, cy = (width - 1) / 2, (height - 1) / 2
    r = (size - 1) / 2                                  # outer radius: the hour marks end here

    def at(fraction, radius):
        """The point `radius` from the centre, `fraction` of a turn clockwise from 12."""
        a = 2 * math.pi * fraction
        return cx + radius * math.sin(a), cy - radius * math.cos(a)

    # Hour marks: 12, 3, 6 and 9 longer
    long_mark = max(2, round(r / 6))
    for h in range(12):
        inner = r - (long_mark if h % 3 == 0 else max(1, long_mark // 2)) + 1
        c.line(*at(h / 12, inner), *at(h / 12, r))

    # Seconds dot, running round the ring of marks
    dot = max(1.0, r / 14)
    c.disc(*at(ss / 60, r - dot), dot)

    # Hands, both moving smoothly between marks; the minute hand reaches the inner end of the long marks
    minute_len = r - long_mark - 1
    hour_len = minute_len * 0.6
    c.line(cx, cy, *at((mm + ss / 60) / 60, minute_len), thickness=2 if r >= 24 else 1)
    c.line(cx, cy, *at((hh % 12 + mm / 60) / 12, hour_len), thickness=2)
    c.disc(cx, cy, 1 if r >= 12 else 0.5)
    return bytes(c.buf), c


class ClockTime:
    """Local time, or a fast clock running `speed` times real time from `start` ("HH:MM")."""

    def __init__(self, speed=1.0, start=None):
        self.speed, self.t0 = speed, time.time()
        self.base = None                            # None: plain local time
        if start:
            h, m, s = ([int(v) for v in start.split(":")] + [0])[:3]
            self.base = h * 3600 + m * 60 + s
        elif speed != 1.0:
            lt = time.localtime(self.t0)
            self.base = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec + self.t0 % 1

    def now(self):
        if self.base is None:
            lt = time.localtime()
            return lt.tm_hour, lt.tm_min, lt.tm_sec
        s = int(self.base + (time.time() - self.t0) * self.speed) % 86400
        return s // 3600, s // 60 % 60, s % 60


class Target:
    """One display: its size, the last image sent, and its current problem (each reported once)."""

    def __init__(self, host, udp, full):
        self.dev, self.udp, self.full = mrroip.Device(host), udp, full
        self.size, self.last, self.problem = None, None, "starting"   # so the first success is reported

    def report(self, problem):
        if problem != self.problem:
            print(f"{self.dev.ip}: {problem}" if problem else f"{self.dev.ip}: showing the clock")
            self.problem = problem

    def send(self, frame_for_size):
        try:
            if self.size is None:
                self.size = self.dev.image_size()
            data = frame_for_size(*self.size)
            if data == self.last:
                return
            if self.full:
                reply = self.dev.show_image(data, udp=self.udp)
            else:
                reply = self.dev.update_image(data, udp=self.udp)
                if reply is None:
                    return
        except ValueError as e:                 # not a display, or a panel too small for a face
            self.report(f"not shown: {e}")
            return
        except OSError as e:                    # unreachable or timed out; it may come back with another panel
            self.size = None
            self.report(f"not shown: {e}")
            return
        if reply.get("accepted"):
            self.last = data
            self.report(None)
        else:
            self.last = None                    # try again next tick
            fault = reply.get("state", {}).get("fault")
            self.report(f"rejected: {reply.get('error')}" + (f" (fault {fault})" if fault else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hosts", nargs="*", help="display addresses")
    ap.add_argument("--speed", type=float, default=1.0, help="fast clock: model minutes per real minute")
    ap.add_argument("--start", help="fast clock start time HH:MM[:SS] (default: now)")
    ap.add_argument("--udp", action="store_true", help="send over UDP instead of HTTP")
    ap.add_argument("--full", action="store_true", help="always send whole images, no partial updates")
    ap.add_argument("--preview", metavar="WxH", help="print one frame for this panel size and exit")
    args = ap.parse_args()

    clock = ClockTime(args.speed, args.start)
    if args.preview:
        w, h = (int(v) for v in args.preview.lower().split("x"))
        print(render(w, h, *clock.now())[1].text())
        return
    if not args.hosts:
        ap.error("give at least one display address, or --preview WxH")

    targets = [Target(h, args.udp, args.full) for h in args.hosts]
    started = time.time()
    tick = max(0.25, 1.0 / max(1.0, args.speed))      # a fast clock's seconds run faster; unchanged frames are not sent
    try:
        while True:
            for t in targets:
                t.send(lambda w, h: render(w, h, *clock.now())[0])
            time.sleep(tick - time.time() % tick + 0.01)            # wake just after the next tick
    except KeyboardInterrupt:
        seconds = max(1.0, time.time() - started)
        print()
        for t in targets:
            print(f"{t.dev.ip}: {t.dev.tx_bytes} bytes sent in {seconds:.0f} s ({t.dev.tx_bytes / seconds:.0f} bytes/s)")


if __name__ == "__main__":
    main()
