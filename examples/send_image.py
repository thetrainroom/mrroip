#!/usr/bin/env python3
"""
send_image.py — show a picture, text or test pattern on an MRRoIP display, using the mrroip package.

    python3.12 send_image.py 192.168.10.164 --file ../../esp32/oled/tools/images/testcard.png
    python3.12 send_image.py 192.168.10.164 --text "Gleis 3"
    python3.12 send_image.py 192.168.10.164 --pattern checker --udp

--text and --file need Pillow.
"""

import argparse
import sys
from pathlib import Path

try:
    import mrroip
except ImportError:                     # not installed: use the package in this repository
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import mrroip


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="picture: scaled to fit, centred, thresholded")
    source.add_argument("--text", help='text; "\\n" starts a new line')
    source.add_argument("--pattern", choices=["border", "checker"])
    ap.add_argument("--udp", action="store_true", help="send over UDP instead of HTTP")
    args = ap.parse_args()

    dev = mrroip.Device(args.host)
    w, h = dev.image_size()
    if args.file:
        data = mrroip.image.picture(args.file, w, h)
    elif args.text:
        data = mrroip.image.text(args.text.replace("\\n", "\n"), w, h)
    else:
        data = mrroip.image.pattern(args.pattern, w, h)

    reply = dev.show_image(data, udp=args.udp)
    if reply.get("accepted"):
        print(f"{w}x{h}: shown, crc32 {reply['state']['profile']['image']['crc32']}")
    else:
        print(f"{w}x{h}: rejected: {reply.get('error')} {reply.get('details', '')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
