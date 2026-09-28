# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
Uncompressed pictures over RTP in the RFC 4175 layout (../../../esp32/oled/MRROIP-PLAN.md questions 15 and 16).

A packet is the 12-byte RTP header, then the payload header: the extended sequence number, then one line header
per piece of a pixel row (length, field and line number, continuation and offset), then the pixels of those
pieces. The marker bit is set on a frame's last packet.

Formats: "rgb565be" is MRRoIP's own 2-byte pixel group, the byte order the panels want; "rgb" is RFC 4175's
standard 8-bit RGB, which GStreamer and FFmpeg can send (rtpvrawpay), and which the endpoint converts.
"""

import random
import socket
import struct
import time
from collections.abc import Sequence
from ipaddress import IPv4Address
from typing import Final

from . import protocol

RTP_VERSION: Final = 2
DEFAULT_PAYLOAD_TYPE: Final = 96
#: one payload type per format, so a capture identifies the pixels (protocol.py)
PAYLOAD_TYPES: Final = protocol.RTP_PAYLOAD_TYPES
#: what a session description calls each format; RFC 4175 names the standard one, RGB565 is MRRoIP's own
SAMPLING: Final[dict[str, tuple[str, int]]] = {"rgb": ("RGB", 8), "rgb565be": ("RGB565", 16)}
CLOCK_HZ: Final = 90000
#: leap seconds between TAI and UTC (37 since 2017). ST 2110 senders count their 90 kHz clock from the TAI
#: epoch, so an analyser can compare a stream's timestamps with its own clock.
TAI_UTC_OFFSET_S: Final = 37
#: octets per pixel group; both formats here carry one pixel per group
PGROUP: Final[dict[str, int]] = {"rgb565be": 2, "rgb": 3}


class Sender:
    """Sends frames to an endpoint's stream port. One instance is one stream: it keeps the sequence numbers."""

    def __init__(self, ip: IPv4Address, port: int, width: int, height: int, fmt: str = "rgb565be",
                 payload_type: int | None = None, mtu: int = 1400, ssrc: int | None = None,
                 sock: socket.socket | None = None, fps: float = 0, tai_offset: float = TAI_UTC_OFFSET_S) -> None:
        if fmt not in PGROUP:
            raise ValueError(f"unknown format {fmt!r}")
        payload_type = PAYLOAD_TYPES.get(fmt, DEFAULT_PAYLOAD_TYPE) if payload_type is None else payload_type
        self.ip, self.port, self.width, self.height, self.fmt = ip, port, width, height, fmt
        self.payload_type, self.mtu = payload_type, mtu
        self.ssrc = ssrc if ssrc is not None else random.getrandbits(32)
        self.sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = random.getrandbits(16)
        self.frames = 0
        self.packets = 0
        self.tx_bytes = 0
        #: packets are spread over the frame period, as a video sender paces them; 0 sends them back to back,
        #: which a small endpoint cannot drain while it is painting
        self.fps = fps
        self._next_packet: float | None = None
        self._pace = 0.0
        #: 90 kHz from the TAI epoch, stepped by the nominal frame period, as a video sender does: an analyser
        #: then sees equal deltas and a timestamp that matches its own clock, whatever the network did
        self.tai_offset = tai_offset
        self._timestamp: int | None = None

    def close(self) -> None:
        self.sock.close()

    def sdp(self, source_ip: IPv4Address = IPv4Address(0), name: str = "MRRoIP video") -> str:
        """A session description for this stream: for Wireshark's "Decode As", for ffplay, or for documentation.
        RFC 4175 has no RGB565, so that sampling name is MRRoIP's own, like the payload type."""
        sampling, depth = SAMPLING[self.fmt]
        return "\n".join([
            "v=0",
            f"o=- 0 0 IN IP4 {source_ip}",
            f"s={name}",
            f"c=IN IP4 {self.ip}",
            "t=0 0",
            f"m=video {self.port} RTP/AVP {self.payload_type}",
            f"a=rtpmap:{self.payload_type} raw/{CLOCK_HZ}",
            f"a=fmtp:{self.payload_type} sampling={sampling}; width={self.width}; height={self.height}; "
            f"depth={depth}; colorimetry=BT709",
            f"a=framerate:{self.fps or 0}",
            "",
        ])

    def _packet(self, timestamp: int, marker: bool, lines: Sequence[tuple[int, int, int]], payload: bytes) -> None:
        """lines: (length, line_number, offset) per piece, in the order their pixels follow in `payload`."""
        self.seq = (self.seq + 1) & 0xFFFF
        header = struct.pack("!BBHII", RTP_VERSION << 6, (marker << 7) | self.payload_type,
                             self.seq, timestamp & 0xFFFFFFFF, self.ssrc)
        out = [header, struct.pack("!H", 0)]            # extended sequence number: one stream, so it stays 0
        for i, (length, line, offset) in enumerate(lines):
            last = (i == len(lines) - 1)
            out.append(struct.pack("!HHH", length, line & 0x7FFF, (0 if last else 0x8000) | (offset & 0x7FFF)))
        out.append(payload)
        datagram = b"".join(out)
        if self._next_packet is not None:
            delay = self._next_packet - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        self.sock.sendto(datagram, (str(self.ip), self.port))
        if self._pace:
            self._next_packet = max(self._next_packet or 0, time.monotonic()) + self._pace
        self.packets += 1
        self.tx_bytes += len(datagram)

    def frame_timestamp(self) -> int:
        """The 90 kHz timestamp for the next frame: the TAI clock at the start, then the nominal frame period."""
        if self._timestamp is None:
            self._timestamp = int((time.time() + self.tai_offset) * CLOCK_HZ) & 0xFFFFFFFF
        elif self.fps:
            self._timestamp = (self._timestamp + round(CLOCK_HZ / self.fps)) & 0xFFFFFFFF
        else:
            self._timestamp = int((time.time() + self.tai_offset) * CLOCK_HZ) & 0xFFFFFFFF
        return self._timestamp

    def send_frame(self, data: bytes, timestamp: int | None = None) -> None:
        """One frame: `data` is width x height pixels in this stream's format, row-major. Every packet of a frame
        carries the same timestamp; the marker bit sits on the last one."""
        pgroup = PGROUP[self.fmt]
        if self.fps:
            packets = -(-(self.width * self.height * pgroup) // (self.mtu - 20))
            self._pace = 1.0 / self.fps / max(1, packets)
            self._next_packet = self._next_packet or time.monotonic()
        else:
            self._pace = 0.0
        stride = self.width * pgroup
        if len(data) != stride * self.height:
            raise ValueError(f"{len(data)} bytes, expected {stride * self.height}")
        if timestamp is None:
            timestamp = self.frame_timestamp()
        # Each packet carries whole rows while they fit, then part of a row
        room = self.mtu - 12 - 2                        # RTP header and extended sequence number
        lines: list[tuple[int, int, int]] = []
        payload, line, offset = bytearray(), 0, 0
        while line < self.height:
            left = (self.width - offset) * pgroup
            space = room - 6 * (len(lines) + 1) - len(payload)
            if space < pgroup:                          # no room for another piece: send what we have
                self._packet(timestamp, False, lines, bytes(payload))
                lines, payload = [], bytearray()
                continue
            take = min(left, space - space % pgroup)
            start = line * stride + offset * pgroup
            payload += data[start: start + take]
            lines.append((take, line, offset))
            offset += take // pgroup
            if offset >= self.width:
                line += 1
                offset = 0
        self._packet(timestamp, True, lines, bytes(payload))     # the marker ends the frame
        self.frames += 1
