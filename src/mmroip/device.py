"""
One MMRoIP endpoint over HTTP and UDP (MMROIP-CORE-SPEC.md §7–§9.6).

Device-agnostic: nothing here knows what kind of device it is talking to. What the endpoint contains
and accepts comes from its /definition.
"""

import json
import socket
import time
import urllib.error
import urllib.request

from . import image, protocol


class Device:
    """One MMRoIP endpoint, reachable over HTTP and UDP."""

    def __init__(self, ip, udp_port=protocol.UDP_PORT, timeout=4.0, seq_start=None):
        self.ip, self.udp_port, self.timeout = ip, udp_port, timeout
        # seq must grow per sender address (§9.5). Starting from a millisecond clock keeps several programs
        # on one host in order; pass seq_start to count from a fixed value instead.
        self.seq = int(time.time() * 1000) if seq_start is None else seq_start
        self.dfn = None
        self.tx_bytes = 0           # request bytes sent (HTTP bodies and datagrams), for measuring traffic
        self._image = None          # (image, crc32) last confirmed by update_image

    # -- HTTP ----------------------------------------------------------
    def req(self, path, body=None, method=None):
        """Returns (status, JSON body); HTTP errors are returned, not raised."""
        url = f"http://{self.ip}{path}"
        data = json.dumps(body).encode() if body is not None else None
        self.tx_bytes += len(data or b"")
        m = method or ("POST" if data else "GET")
        r = urllib.request.Request(url, data=data, method=m,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as f:
                return f.status, json.loads(f.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:    return e.code, json.loads(raw or b"{}")
            except Exception:
                    return e.code, {"_raw": raw[:400].decode("utf8", "replace")}

    def definition(self):
        self.dfn = self.req("/definition")[1]
        return self.dfn

    def get_config(self):   return self.req("/config")[1]
    def cfg(self):          return self.get_config().get("config", {})
    def meta(self):         return self.get_config().get("_meta", {})
    def state(self):        return self.req("/state")[1]
    def pstate(self):       return self.state().get("profile", {})

    def set_config(self, **kw):
        return self.req("/config", kw)

    def control(self, **kw):
        self.seq += 1
        kw.setdefault("seq", self.seq)
        kw.setdefault("ts", int(time.monotonic() * 1000))
        return self.req("/control", kw)[1]

    # -- UDP -----------------------------------------------------------
    def control_udp(self, raw=None, **kw):
        if raw is None:
            self.seq += 1
            kw.setdefault("seq", self.seq)
            kw.setdefault("ts", int(time.monotonic() * 1000))
            raw = json.dumps(kw).encode()
        self.tx_bytes += len(raw)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(self.timeout)
        try:
            s.sendto(raw, (self.ip, self.udp_port))
            data, _ = s.recvfrom(4096)
            return json.loads(data)
        finally:
            s.close()

    # -- convenience ---------------------------------------------------
    @property
    def device_class(self):
        return (self.dfn or {}).get("device_class")

    @property
    def modes(self):
        return (self.dfn or {}).get("capabilities", {}).get("modes", [])

    def param(self, name):
        for p in (self.dfn or {}).get("parameters", []):
            if p["name"] == name:
                return p
        return None

    def wait(self, pred, limit=30.0, poll=0.25):
        """Poll /state until pred(state) is true. Returns the state or None."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < limit:
            st = self.state()
            if pred(st):
                return st
            time.sleep(poll)
        return None

    def wait_idle(self, limit=40.0):
        return self.wait(lambda s: not s.get("busy"), limit)

    def quiesce(self):
        """Known-good starting point that needs no profile knowledge."""
        self.control(mode="reset")
        self.control(mode="release")

    # -- display profile ----------------------------------------------
    def image_size(self):
        """(width, height) of the display profile's image object."""
        return image.image_size(self.dfn or self.definition())

    def show_image(self, data, mode="show", udp=False):
        """Show 1-bit image bytes in the wire format (see mmroip.image). Returns the control response."""
        objects = {"image": image.encode(data)}
        if udp:
            return self.control_udp(mode=mode, objects=objects)
        return self.control(mode=mode, objects=objects)

    def patch_image(self, base_crc32, rects, mode="show", udp=False):
        """Replace rectangles of the image on screen; rects = [(x, y, w, h, bytes)], each rectangle an image of its
        own. Refused with details reason "stale_base" unless base_crc32 is the image on screen."""
        objects = {"image": {"base_crc32": base_crc32, "rects": [
            {"x": x, "y": y, "w": w, "h": h, "data": image.encode(d)} for x, y, w, h, d in rects]}}
        if udp:
            return self.control_udp(mode=mode, objects=objects)
        return self.control(mode=mode, objects=objects)

    def update_image(self, data, mode="show", udp=False):
        """Show an image, sending only the rectangles that differ from the image this method showed last. Sends the
        whole image the first time, when the display shows something else (stale_base), when it does not take
        rectangles, or when they would not be smaller. Returns the control response, or None if nothing changed."""
        w, h = self.image_size()
        if self._image is not None:
            old, crc = self._image
            if old == data:
                return None
            obj = next((o for o in self.dfn.get("objects", []) if o.get("id") == "image"), {})
            max_rects = obj.get("profile", {}).get("max_rects", 0)
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
    def put_image(self, data, x=None, y=None, w=None, h=None, base=None):
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
        r = urllib.request.Request(f"http://{self.ip}/objects/image{query}", data=data, method="PUT",
                                   headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as f:
                return json.loads(f.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:    return json.loads(raw or b"{}")
            except Exception:
                    return {"accepted": False, "_raw": raw[:400].decode("utf8", "replace")}

    def image_tile_px(self):
        """The tile size the endpoint groups its picture into, or 0 if it takes no uploads."""
        obj = next((o for o in (self.dfn or self.definition()).get("objects", []) if o.get("id") == "image"), {})
        return obj.get("profile", {}).get("tile_px", 0)

    def update_image_rgb565(self, data):
        """Show an rgb565be picture, sending only the tiles that differ from the last one this method sent.
        Falls back to the whole picture the first time, when the endpoint shows something else, or when the
        rectangles would not be smaller. Returns the control response, or None if nothing changed."""
        w, h = self.image_size()
        tile = self.image_tile_px() or 20
        if self._image is not None:
            old, previous_id = self._image
            if old == data:
                return None
            rects = image.changed_tiles(old, data, w, h, tile)
            if rects and sum(r[2] * r[3] * 2 for r in rects) < len(data):
                reply = None
                for (rx, ry, rw, rh) in rects:
                    reply = self.put_image(image.crop_rgb565(data, w, rx, ry, rw, rh), rx, ry, rw, rh,
                                           base=previous_id)
                    if not reply.get("accepted"):
                        break
                    previous_id = ((reply.get("state") or {}).get("profile") or {}).get("image", {}).get("id")
                if reply is not None and reply.get("accepted"):
                    self._remember_rgb565(data, reply)
                    return reply
        reply = self.put_image(data)
        self._remember_rgb565(data, reply)
        return reply

    def _remember_rgb565(self, data, reply):
        w, h = self.image_size()
        shown = ((reply.get("state") or {}).get("profile") or {}).get("image") or {}
        ok = reply.get("accepted") and shown.get("id") == image.image_id(data, w, h, self.image_tile_px() or 20)
        self._image = (data, shown.get("id")) if ok else None

    def _remember(self, data, reply):
        shown = ((reply.get("state") or {}).get("profile") or {}).get("image") or {}
        ok = reply.get("accepted") and shown.get("crc32") == image.crc32(data)
        self._image = (data, shown["crc32"]) if ok else None
