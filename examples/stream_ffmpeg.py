# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
#!/usr/bin/env python3
"""
stream_ffmpeg.py — stream anything FFmpeg can read to a colour MRRoIP endpoint.

    python3.12 stream_ffmpeg.py 192.168.10.165 --source "smptebars=size=240x280:rate=5" --seconds 10
    python3.12 stream_ffmpeg.py 192.168.10.165 --file clip.mp4 --fps 5
    python3.12 stream_ffmpeg.py 192.168.10.165 --file photo.jpg --loop --fps 5

FFmpeg scales the picture to the panel, sets the rate and converts to rgb565be; this script starts the stream,
packetises the frames in the RFC 4175 layout and stops the stream at the end.

FFmpeg can also packetise RFC 4175 itself (`-c:v bitpacked -f rtp`), but only as YCbCr 4:2:2 10-bit, which this
endpoint does not take yet — hence the pipe.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

try:
    import mrroip
except ImportError:                     # not installed: use the package in this repository
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import mrroip


def ffmpeg_command(args, width, height):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if args.file:
        if args.loop:
            command += ["-stream_loop", "-1"]
        command += ["-re", "-i", args.file]
    else:
        command += ["-f", "lavfi", "-i", args.source]
    if args.seconds:
        command += ["-t", str(args.seconds)]
    command += ["-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={args.fps}",
                "-pix_fmt", "rgb565be", "-f", "rawvideo", "-"]
    return command


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--source", default="testsrc=size=240x280:rate=5", help="an FFmpeg lavfi source")
    source.add_argument("--file", help="a video or picture file")
    ap.add_argument("--loop", action="store_true", help="repeat the file")
    ap.add_argument("--fps", type=float, default=5, help="frames a second (5 is what a 240x280 panel keeps up with)")
    ap.add_argument("--seconds", type=float, default=10, help="how long to stream; 0 means until interrupted")
    ap.add_argument("--port", type=int, default=5004)
    args = ap.parse_args()

    device = mrroip.Device(args.host, timeout=15)
    width, height = device.image_size()
    frame_bytes = width * height * 2

    reply = device.control(mode="show", objects={"stream": {"port": args.port, "format": "rgb565be"}})
    if not reply.get("accepted"):
        print(f"the endpoint did not start a stream: {reply.get('error')} {reply.get('details', '')}")
        return 1
    before = device.pstate().get("stream", {})

    sender = mrroip.rtp.Sender(device.ip, args.port, width, height, "rgb565be", fps=args.fps)
    command = ffmpeg_command(args, width, height)
    print(" ".join(command))
    ffmpeg = subprocess.Popen(command, stdout=subprocess.PIPE)
    assert ffmpeg.stdout is not None             # stdout=PIPE
    started = time.time()
    try:
        while True:
            frame = ffmpeg.stdout.read(frame_bytes)
            if len(frame) < frame_bytes:
                break
            sender.send_frame(frame)
    except KeyboardInterrupt:
        pass
    finally:
        ffmpeg.terminate()
        ffmpeg.wait()
        seconds = max(0.001, time.time() - started)
        time.sleep(1.0)
        after = device.pstate().get("stream", {})
        device.control(mode="show", objects={"stream": "off"})
        sender.close()
        print(f"sent {sender.frames} frames ({sender.packets} packets, {sender.tx_bytes / 1024:.0f} KB) in "
              f"{seconds:.1f} s = {sender.frames / seconds:.1f} fps, {sender.tx_bytes * 8 / seconds / 1e6:.1f} Mbit/s")
        print(f"endpoint received {after.get('frames', 0) - before.get('frames', 0)} whole frames, "
              f"lost {after.get('lost_packets', 0) - before.get('lost_packets', 0)} packets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
