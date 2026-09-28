# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
Protocol constants (MRROIP-1.md §2.4, §5.3, §9.3). The name is spelled here and nowhere else in
the package, so a rename is a one-line diff.
"""

from typing import Final

NAME: Final = "MRRoIP"
TOKEN: Final = "mrroip"
VERSION: Final = "0.1"

UDP_PORT: Final = 5300
WHOIS_PORT: Final = 8266
SSDP_ADDR: Final[tuple[str, int]] = ("239.255.255.250", 1900)
SSDP_ST: Final = "urn:schemas-mrroip-org:device:Endpoint:1"
HEADER_PREFIX: Final = "X-MRROIP-"        # X-MRROIP-ID, -NAME, -TYPE, -CLASS (§6.1)
MDNS_SERVICE: Final = "_mrroip"           # + "._tcp" (§6.2)

#: One dynamic RTP payload type per pixel format of a video stream (plan question 16). 96 is what GStreamer and
#: FFmpeg send for RFC 4175 video, so standard 8-bit RGB keeps it; MRRoIP's own two-byte pixel group gets its own
#: number, which lets a capture tell the two apart without a session description.
RTP_PAYLOAD_TYPES: Final[dict[str, int]] = {"rgb": 96, "rgb565be": 98}

CORE_MODES: Final[tuple[str, ...]] = ("estop", "reset", "release", "hold")

BODY_MAX: Final = 4096                     # §2.3: the limit on every reserved path
REPLAY_WINDOW_MS: Final = 5000             # §9.5
