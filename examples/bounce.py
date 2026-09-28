#!/usr/bin/env python3
"""
bounce.py — a box bouncing around a colour endpoint's screen, to watch motion and compare the two ways of
sending it (../../esp32/oled/MRROIP-PLAN.md question 16).

    python3.12 bounce.py 192.168.10.165                      # tiles: only what changed, over HTTP
    python3.12 bounce.py 192.168.10.165 --mode stream --fps 5  # whole frames over RTP
    python3.12 bounce.py 192.168.10.165 --seconds 60 --box 30 --speed 12

`tiles` sends the tiles that differ from the previous picture, so a small box costs a few kilobytes a frame
instead of 134; `stream` sends every frame whole and is limited by what the panel can take (about 5 frames a
second at 240x280).
"""

import argparse
import sys
import time
from pathlib import Path

try:
    import mrroip
except ImportError:                     # not installed: use the package in this repository
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import mrroip

from mrroip import image

COLOURS = [(255, 80, 80), (80, 255, 120), (110, 160, 255), (255, 220, 80), (255, 120, 255), (120, 255, 255)]


def background(width, height):
    """A dark backdrop with a one-pixel frame, so the box has something to move over."""
    dark, edge = image.rgb565(0, 0, 40), image.rgb565(60, 60, 90)
    rows = [edge * width if y in (0, height - 1) else edge + dark * (width - 2) + edge for y in range(height)]
    return b"".join(rows)


def draw_box(frame, width, x, y, size, colour):
    """The box painted into a copy of `frame`; rows are sliced in, so a frame costs microseconds."""
    out = bytearray(frame)
    row = image.rgb565(*colour) * size
    for line in range(size):
        at = (y + line) * width * 2 + x * 2
        out[at: at + size * 2] = row
    return bytes(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    ap.add_argument("--mode", choices=["tiles", "stream"], default="tiles")
    ap.add_argument("--fps", type=float, default=12, help="frames a second to aim for")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--box", type=int, default=40, help="the box's size in pixels")
    ap.add_argument("--speed", type=int, default=9, help="pixels per frame")
    ap.add_argument("--port", type=int, default=5004)
    ap.add_argument("--rects", type=int, default=1,
                    help="tiles mode: rectangles per frame. 1 erases the old box and draws the new one in a "
                         "single write, which is what stops the seam; try more to see the difference")
    args = ap.parse_args()

    device = mrroip.Device(args.host, timeout=15)
    width, height = device.image_size()
    back = background(width, height)
    x, y = width // 3, height // 3
    dx, dy = args.speed, args.speed
    colour = 0

    sender = None
    if args.mode == "stream":
        if not device.control(mode="show", objects={"stream": {"port": args.port}}).get("accepted"):
            print("the endpoint did not start a stream")
            return 1
        sender = mrroip.rtp.Sender(args.host, args.port, width, height, "rgb565be", fps=args.fps)
    else:
        device.put_image(back)          # a known starting picture, so the first tiles have a base

    frames, refusals = 0, 0
    started = time.time()
    period = 1.0 / args.fps
    try:
        while time.time() - started < args.seconds:
            frame_start = time.time()
            frame = draw_box(back, width, x, y, args.box, COLOURS[colour])
            if sender:
                sender.send_frame(frame)
            else:
                reply = device.update_image_rgb565(frame, max_rects=args.rects)
                if reply is not None and not reply.get("accepted"):
                    refusals += 1
            frames += 1

            x += dx
            y += dy
            if x <= 0 or x + args.box >= width:
                dx, x = -dx, max(0, min(x, width - args.box))
                colour = (colour + 1) % len(COLOURS)
            if y <= 0 or y + args.box >= height:
                dy, y = -dy, max(0, min(y, height - args.box))
                colour = (colour + 1) % len(COLOURS)

            rest = period - (time.time() - frame_start)
            if rest > 0:
                time.sleep(rest)
    except KeyboardInterrupt:
        pass
    finally:
        seconds = max(0.001, time.time() - started)
        if sender:
            state = device.pstate().get("stream", {})
            device.control(mode="show", objects={"stream": "off"})
            sender.close()
            print(f"{frames} frames in {seconds:.1f} s = {frames / seconds:.1f} fps, "
                  f"{sender.tx_bytes * 8 / seconds / 1e6:.1f} Mbit/s; the endpoint completed "
                  f"{state.get('frames', 0)} frames, lost {state.get('lost_packets', 0)} packets")
        else:
            print(f"{frames} frames in {seconds:.1f} s = {frames / seconds:.1f} fps, "
                  f"{device.tx_bytes / 1024:.0f} KB sent = {device.tx_bytes / max(1, frames) / 1024:.1f} KB a frame"
                  f"{f', {refusals} refused' if refusals else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
