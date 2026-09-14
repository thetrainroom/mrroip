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

    # -- HTTP ----------------------------------------------------------
    def req(self, path, body=None, method=None):
        """Returns (status, JSON body); HTTP errors are returned, not raised."""
        url = f"http://{self.ip}{path}"
        data = json.dumps(body).encode() if body is not None else None
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
