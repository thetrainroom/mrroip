# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
One MRRoIP endpoint over HTTP and UDP (MRROIP-1.md §7–§9.8).

Device-agnostic: nothing here knows what kind of device it is talking to. What the endpoint contains
and accepts comes from its /definition.
"""

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from . import image, protocol
from ._types import Json, JsonObject, Rect, as_object


def _object(value: Json) -> JsonObject:
    """value if it is a JSON object, else an empty one: for walking documents an endpoint may leave short."""
    return as_object(value) or {}


class Device:
    """One MRRoIP endpoint, reachable over HTTP and UDP."""

    def __init__(self, ip: ipaddress.IPv4Address | str, udp_port: int = protocol.UDP_PORT, timeout: float = 4.0,
                 seq_start: int | None = None, http_port: int | None = None) -> None:
        """ip is an address, or text: "192.168.1.5", or "192.168.1.5:8080" for an endpoint serving HTTP on
        another port (§5.3)."""
        if isinstance(ip, str):
            text, _, port = ip.partition(":")
            ip = ipaddress.IPv4Address(text)
            if http_port is None and port:
                http_port = int(port)
        http_port = 80 if http_port is None else http_port
        self.ip: ipaddress.IPv4Address = ip
        self.http_port, self.udp_port, self.timeout = http_port, udp_port, timeout
        self.host = str(ip) if http_port == 80 else f"{ip}:{http_port}"
        # seq must grow per sender address (§9.5). Starting from a millisecond clock keeps several programs
        # on one host in order; pass seq_start to count from a fixed value instead.
        self.seq: int = int(time.time() * 1000) if seq_start is None else seq_start
        self.dfn: JsonObject | None = None
        self.tx_bytes = 0           # request bytes sent (HTTP bodies and datagrams), for measuring traffic
        self._image: tuple[bytes, str | None] | None = None   # (image, crc32 or id) last confirmed as shown

    # -- HTTP ----------------------------------------------------------
    def req(self, path: str, body: JsonObject | None = None, method: str | None = None) -> tuple[int, JsonObject]:
        """Returns (status, JSON body); HTTP errors are returned, not raised."""
        url = f"http://{self.host}{path}"
        data = json.dumps(body).encode() if body is not None else None
        self.tx_bytes += len(data or b"")
        m = method or ("POST" if data else "GET")
        r = urllib.request.Request(url, data=data, method=m,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as f:
                return f.status, _object(json.loads(f.read() or b"{}"))
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, _object(json.loads(raw or b"{}"))
            except ValueError:
                return e.code, {"_raw": raw[:400].decode("utf8", "replace")}

    def definition(self) -> JsonObject:
        self.dfn = self.req("/definition")[1]
        return self.dfn

    def get_config(self) -> JsonObject:
        return self.req("/config")[1]

    def cfg(self) -> JsonObject:
        return _object(self.get_config().get("config"))

    def meta(self) -> JsonObject:
        return _object(self.get_config().get("_meta"))

    def state(self) -> JsonObject:
        return self.req("/state")[1]

    def pstate(self) -> JsonObject:
        return _object(self.state().get("profile"))

    def set_config(self, **kw: Json) -> tuple[int, JsonObject]:
        """POST /config: parameters, plus persist=, if_version= or factory_reset= (§8.2)."""
        return self.req("/config", kw)

    def control(self, **kw: Json) -> JsonObject:
        """POST /control with seq and ts filled in (§9.2)."""
        self.seq += 1
        kw.setdefault("seq", self.seq)
        kw.setdefault("ts", int(time.monotonic() * 1000))
        return self.req("/control", kw)[1]

    # -- UDP -----------------------------------------------------------
    def control_udp(self, raw: bytes | None = None, **kw: Json) -> JsonObject:
        """The same message as one datagram; raw sends bytes as they are. Waits for the reply."""
        if raw is None:
            self.seq += 1
            kw.setdefault("seq", self.seq)
            kw.setdefault("ts", int(time.monotonic() * 1000))
            raw = json.dumps(kw).encode()
        self.tx_bytes += len(raw)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        try:
            s.sendto(raw, (str(self.ip), self.udp_port))
            data, _ = s.recvfrom(4096)
            return _object(json.loads(data))
        finally:
            s.close()

    # -- convenience ---------------------------------------------------
    @property
    def device_class(self) -> str | None:
        return (self.dfn or {}).get("device_class")

    @property
    def modes(self) -> list[str]:
        return _object((self.dfn or {}).get("capabilities")).get("modes", [])

    def param(self, name: str) -> JsonObject | None:
        for p in (self.dfn or {}).get("parameters", []):
            if p["name"] == name:
                return p
        return None

    def wait(self, pred: Callable[[JsonObject], object], limit: float = 30.0, poll: float = 0.25) -> JsonObject | None:
        """Poll /state until pred(state) is true. Returns the state or None."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            st = self.state()
            if pred(st):
                return st
            time.sleep(poll)
        return None

    def wait_idle(self, limit: float = 40.0) -> JsonObject | None:
        return self.wait(lambda s: not s.get("busy"), limit)

    def quiesce(self) -> None:
        """Known-good starting point that needs no profile knowledge."""
        self.control(mode="reset")
        self.control(mode="release")

    # -- display profile ----------------------------------------------
    def image_size(self) -> tuple[int, int]:
        """(width, height) of the display profile's image object."""
        return image.image_size(self.dfn or self.definition())

    def show_image(self, data: bytes, mode: str = "show", udp: bool = False) -> JsonObject:
        """Show 1-bit image bytes in the wire format (see mrroip.image). Returns the control response."""
        objects = {"image": image.encode(data)}
        if udp:
            return self.control_udp(mode=mode, objects=objects)
        return self.control(mode=mode, objects=objects)

    def patch_image(self, base_crc32: str | None, rects: Sequence[tuple[int, int, int, int, bytes]],
                    mode: str = "show", udp: bool = False) -> JsonObject:
        """Replace rectangles of the image on screen; rects = [(x, y, w, h, bytes)], each rectangle an image of its
        own. Refused with details reason "stale_base" unless base_crc32 is the image on screen."""
        objects = {"image": {"base_crc32": base_crc32, "rects": [
            {"x": x, "y": y, "w": w, "h": h, "data": image.encode(d)} for x, y, w, h, d in rects]}}
        if udp:
            return self.control_udp(mode=mode, objects=objects)
        return self.control(mode=mode, objects=objects)

    def update_image(self, data: bytes, mode: str = "show", udp: bool = False) -> JsonObject | None:
        """Show an image, sending only the rectangles that differ from the image this method showed last. Sends the
        whole image the first time, when the display shows something else (stale_base), when it does not take
        rectangles, or when they would not be smaller. Returns the control response, or None if nothing changed."""
        w, h = self.image_size()
        if self._image is not None:
            old, crc = self._image
            if old == data:
                return None
            max_rects = self._max_rects()
            rects = image.changed_rects(old, data, w, h, max_rects=max_rects) if max_rects else []
            parts = [(x, y, rw, rh, image.crop(data, w, x, y, rw, rh)) for x, y, rw, rh in rects]
            if parts and sum(len(p[4]) for p in parts) < len(data):
                reply = self.patch_image(crc, parts, mode, udp)
                reasons = {d.get("reason") for d in reply.get("details", [])}
                if reply.get("accepted") or "stale_base" not in reasons:
                    self._remember(data, reply)
                    return reply
        reply = self.show_image(data, mode, udp)
        self._remember(data, reply)
        return reply

    # -- colour endpoints: pixels by upload (plan question 16) ---------
    def put_image(self, data: bytes, x: int | None = None, y: int | None = None, w: int | None = None,
                  h: int | None = None, base: str | None = None) -> JsonObject:
        """PUT /objects/image: rgb565be pixels, the whole picture or a tile-aligned rectangle (then base is the
        image id underneath). Returns the control response."""
        query = ""
        if x is not None:
            query = f"?x={x}&y={y}&w={w}&h={h}"
        self.seq += 1
        headers = {"Content-Type": "application/octet-stream",
                   protocol.HEADER_PREFIX + "Seq": str(self.seq)}
        if base:
            headers[protocol.HEADER_PREFIX + "Base"] = base
        self.tx_bytes += len(data)
        r = urllib.request.Request(f"http://{self.host}/objects/image{query}", data=data, method="PUT",
                                   headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as f:
                return _object(json.loads(f.read() or b"{}"))
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return _object(json.loads(raw or b"{}"))
            except ValueError:
                return {"accepted": False, "_raw": raw[:400].decode("utf8", "replace")}

    def _image_profile(self) -> JsonObject:
        objects: list[JsonObject] = (self.dfn or self.definition()).get("objects", [])
        for o in objects:
            if o.get("id") == "image":
                return _object(o.get("profile"))
        return {}

    def _max_rects(self) -> int:
        return self._image_profile().get("max_rects", 8)

    def image_tile_px(self) -> int:
        """The tile size the endpoint groups its picture into, or 0 if it takes no uploads."""
        return self._image_profile().get("tile_px", 0)

    def update_image_rgb565(self, data: bytes, max_rects: int | None = None) -> JsonObject | None:
        """Show an rgb565be picture, sending only the tiles that differ from the last one this method sent.
        Falls back to the whole picture the first time, when the endpoint shows something else, or when the
        rectangles would not be smaller. Returns the control response, or None if nothing changed.

        max_rects limits how many rectangles one picture may take; more are merged into their bounding box.
        max_rects=1 sends everything that changed in a single write, which matters for a moving object: with
        several writes the endpoint erases the old position and draws the new one milliseconds apart, and the
        eye sees the seam."""
        w, h = self.image_size()
        tile = self.image_tile_px() or 20
        if self._image is not None:
            old, previous_id = self._image
            if old == data:
                return None
            rects: list[Rect] = image.changed_tiles(old, data, w, h, tile,
                                                    max_rects=max_rects or self._max_rects())
            if rects and sum(r[2] * r[3] * 2 for r in rects) < len(data):
                reply: JsonObject | None = None
                for (rx, ry, rw, rh) in rects:
                    reply = self.put_image(image.crop_rgb565(data, w, rx, ry, rw, rh), rx, ry, rw, rh,
                                           base=previous_id)
                    if not reply.get("accepted"):
                        break
                    previous_id = _shown(reply).get("id")
                if reply is not None and reply.get("accepted"):
                    self._remember_rgb565(data, reply)
                    return reply
        reply = self.put_image(data)
        self._remember_rgb565(data, reply)
        return reply

    def _remember_rgb565(self, data: bytes, reply: JsonObject) -> None:
        w, h = self.image_size()
        shown = _shown(reply)
        ok = reply.get("accepted") and shown.get("id") == image.image_id(data, w, h, self.image_tile_px() or 20)
        self._image = (data, shown.get("id")) if ok else None

    def _remember(self, data: bytes, reply: JsonObject) -> None:
        shown = _shown(reply)
        ok = reply.get("accepted") and shown.get("crc32") == image.crc32(data)
        self._image = (data, shown["crc32"]) if ok else None


def _shown(reply: JsonObject) -> JsonObject:
    """state.profile.image of a control response: what the display reports showing."""
    return _object(_object(_object(reply.get("state")).get("profile")).get("image"))
