"""
Uncompressed pictures over RTP in the RFC 4175 layout (../../oled/MMROIP-PLAN.md questions 15 and 16).

A packet is the 12-byte RTP header, then the payload header: the extended sequence number, then one line header
per piece of a pixel row (length, field and line number, continuation and offset), then the pixels of those
pieces. The marker bit is set on a frame's last packet.

Formats: "rgb565be" is MMRoIP's own 2-byte pixel group, the byte order the panels want; "rgb" is RFC 4175's
standard 8-bit RGB, which GStreamer and FFmpeg can send (rtpvrawpay), and which the endpoint converts.
"""

import random
import socket
import struct
import time

RTP_VERSION = 2
DEFAULT_PAYLOAD_TYPE = 96
CLOCK_HZ = 90000
#: octets per pixel group; both formats here carry one pixel per group
PGROUP = {"rgb565be": 2, "rgb": 3}


class Sender:
    """Sends frames to an endpoint's stream port. One instance is one stream: it keeps the sequence numbers."""

    def __init__(self, ip, port, width, height, fmt="rgb565be", payload_type=DEFAULT_PAYLOAD_TYPE,
                 mtu=1400, ssrc=None, sock=None, fps=0):
        if fmt not in PGROUP:
            raise ValueError(f"unknown format {fmt!r}")
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
        self._next_packet = None

    def close(self):
        self.sock.close()

    def _packet(self, timestamp, marker, lines, payload):
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
        self.sock.sendto(datagram, (self.ip, self.port))
        if self._pace:
            self._next_packet = max(self._next_packet or 0, time.monotonic()) + self._pace
        self.packets += 1
        self.tx_bytes += len(datagram)

    def send_frame(self, data, timestamp=None):
        """One frame: `data` is width x height pixels in this stream's format, row-major."""
        pgroup = PGROUP[self.fmt]
        if self.fps:
            packets = -(-(self.width * self.height * pgroup) // (self.mtu - 20))
            self._pace = 1.0 / self.fps / max(1, packets)
            self._next_packet = self._next_packet or time.monotonic()
        else:
            self._pace = 0
        stride = self.width * pgroup
        if len(data) != stride * self.height:
            raise ValueError(f"{len(data)} bytes, expected {stride * self.height}")
        if timestamp is None:
            timestamp = int(time.monotonic() * CLOCK_HZ)
        # Each packet carries whole rows while they fit, then part of a row
        room = self.mtu - 12 - 2                        # RTP header and extended sequence number
        lines, payload, line, offset = [], bytearray(), 0, 0
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
