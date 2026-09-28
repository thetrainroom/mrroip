# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
Finding MRRoIP endpoints (MRROIP-1.md §6): SSDP, which is normative, the whois probe, which is a
diagnostic for networks that block multicast, and mDNS, which is a convenience.

    ssdp_search()       ask: every endpoint that answers an M-SEARCH
    NotifyListener      listen: the NOTIFY messages endpoints send by themselves (alive, byebye)
    whois()             ask one endpoint, or broadcast
    mdns_browse()       DNS-SD browse for _mrroip._tcp with a one-shot mDNS query

Results are keyed by the endpoint's address. On a computer with several networks, multicast and broadcast
leave through the default interface. Pass `iface` (a local IPv4 address) to use another one, or set
MRROIP_IFACE for code you cannot change, such as the conformance probe. Standard library only.
"""

import json
import os
import random
import socket
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from ipaddress import IPv4Address
from types import TracebackType
from typing import Self

from . import protocol
from ._types import Json, JsonObject, as_object

MDNS_ADDR = ("224.0.0.251", 5353)
MDNS_SERVICE = f"{protocol.MDNS_SERVICE}._tcp.local"

#: SSDP headers, names upper-cased
Headers = dict[str, str]


@dataclass(frozen=True)
class MdnsService:
    """One _mrroip._tcp instance (§6.2)."""
    instance: str
    host: str | None                # <device_name>.local
    port: int | None                # the HTTP port
    txt: dict[str, str]             # id, name, type, class, fw


@dataclass(frozen=True)
class WhoisReply:
    """The whois reply (§6.3): who answers at an address, without fetching /definition."""
    device_id: str
    name: str
    device_type: str
    device_class: str
    ip: IPv4Address
    #: "/definition", or a full URL for an endpoint not serving HTTP on port 80 (§5.3)
    definition: str = "/definition"
    proto: str = protocol.NAME
    version: str = protocol.VERSION

    def to_json(self) -> JsonObject:
        return {"proto": self.proto, "v": self.version, "id": self.device_id, "name": self.name,
                "type": self.device_type, "class": self.device_class, "ip": str(self.ip),
                "definition": self.definition}

    @classmethod
    def from_json(cls, value: Json) -> "WhoisReply | None":
        """None unless value is a whois reply."""
        obj = as_object(value)
        if obj is None:
            return None
        try:
            text = {k: obj[k] for k in ("proto", "v", "id", "name", "type", "class", "ip", "definition")}
            if not all(isinstance(v, str) for v in text.values()):
                return None
            return cls(text["id"], text["name"], text["type"], text["class"], IPv4Address(text["ip"]),
                       text["definition"], text["proto"], text["v"])
        except (KeyError, ValueError):
            return None


@dataclass
class NotifyEvent:
    t: float                    # time.monotonic() when it arrived
    ip: IPv4Address
    nts: str                    # "ssdp:alive" or "ssdp:byebye"
    headers: Headers = field(default_factory=Headers)


def _iface(iface: IPv4Address | None) -> IPv4Address | None:
    if iface is not None:
        return iface
    env = os.environ.get("MRROIP_IFACE")
    return IPv4Address(env) if env else None


def parse_ssdp(data: bytes) -> tuple[str, Headers]:
    """(first line, {HEADER: value}) of an SSDP message."""
    lines = data.decode("utf8", "replace").split("\r\n")
    headers: Headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().upper()] = v.strip()
    return lines[0], headers


def _is_mrroip(headers: Headers) -> bool:
    return (protocol.TOKEN in headers.get("USN", "").lower()
            or protocol.TOKEN in headers.get("ST", "").lower()
            or protocol.TOKEN in headers.get("NT", "").lower()
            or f"{protocol.HEADER_PREFIX}ID" in headers)


def ssdp_search(timeout: float = 6.0, st: str = protocol.SSDP_ST,
                iface: IPv4Address | None = None) -> dict[IPv4Address, Headers]:
    """Returns {address: {header: value}} for every MRRoIP endpoint that answers."""
    iface = _iface(iface)
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {protocol.SSDP_ADDR[0]}:{protocol.SSDP_ADDR[1]}\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           f"ST: {st}\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    if iface:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface.packed)
        s.bind((str(iface), 0))                 # answers come back to this interface
    s.settimeout(1.0)
    found: dict[IPv4Address, Headers] = {}
    t0 = time.monotonic()
    try:
        for _ in range(3):
            s.sendto(msg, protocol.SSDP_ADDR)
            time.sleep(0.15)
        while time.monotonic() - t0 < timeout:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            _, hdr = parse_ssdp(data)
            if _is_mrroip(hdr):
                found[IPv4Address(addr[0])] = hdr
    finally:
        s.close()
    return found


class NotifyListener:
    """
    Collects the SSDP NOTIFY messages MRRoIP endpoints send by themselves, in a background thread.

        with NotifyListener() as listener:
            ...                                     # restart a device, rename it, wait
            for e in listener.events:
                print(e.ip, e.nts)
    """

    def __init__(self, iface: IPv4Address | None = None) -> None:
        self.iface = _iface(iface)
        self.events: list[NotifyEvent] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> Self:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)   # other SSDP listeners may hold the port
        s.bind(("", protocol.SSDP_ADDR[1]))
        membership = socket.inet_aton(protocol.SSDP_ADDR[0]) + (self.iface or IPv4Address(0)).packed
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        s.settimeout(0.2)
        self._sock = s
        self._thread = threading.Thread(target=self._run, args=(s,), name="ssdp-notify", daemon=True)
        self._thread.start()
        return self

    def _run(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            first, hdr = parse_ssdp(data)
            if first.startswith("NOTIFY") and _is_mrroip(hdr):
                with self._lock:
                    self.events.append(NotifyEvent(time.monotonic(), IPv4Address(addr[0]), hdr.get("NTS", ""), hdr))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()

    def since(self, t: float, ip: IPv4Address | None = None, nts: str | None = None) -> list[NotifyEvent]:
        """Events after monotonic time t, optionally for one address and one NTS ("ssdp:alive", "ssdp:byebye")."""
        with self._lock:
            return [e for e in self.events
                    if e.t >= t and (ip is None or e.ip == ip) and (nts is None or e.nts == nts)]

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, kind: type[BaseException] | None, value: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.stop()


def whois(ip: IPv4Address | None = None, timeout: float = 3.0,
          iface: IPv4Address | None = None) -> dict[IPv4Address, WhoisReply]:
    """Diagnostic probe. Unicast if ip is given, otherwise broadcast."""
    iface = _iface(iface)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if iface:
        s.bind((str(iface), 0))
    s.settimeout(0.6)
    target = (str(ip) if ip else "255.255.255.255", protocol.WHOIS_PORT)
    out: dict[IPv4Address, WhoisReply] = {}
    t0 = time.monotonic()
    try:
        for _ in range(3):
            s.sendto(json.dumps({"m": "whois"}).encode(), target)
            time.sleep(0.1)
        while time.monotonic() - t0 < timeout:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            try:
                reply = WhoisReply.from_json(json.loads(data))
            except ValueError:
                continue
            if reply is not None:
                out[IPv4Address(addr[0])] = reply
    finally:
        s.close()
    return out


# ------------------------------------------------------------------ mDNS (RFC 6762 / 6763)

_PTR, _TXT, _SRV, _A = 12, 16, 33, 1


def _encode_name(name: str) -> bytes:
    return b"".join(bytes([len(label)]) + label.encode() for label in name.rstrip(".").split(".")) + b"\0"


def _read_name(msg: bytes, pos: int) -> tuple[str, int]:
    """(name, position after it), following compression pointers."""
    labels: list[str] = []
    jumped, end = False, pos
    for _ in range(64):
        length = msg[pos]
        if length & 0xC0 == 0xC0:
            if not jumped:
                end = pos + 2
            pos, jumped = ((length & 0x3F) << 8) | msg[pos + 1], True
            continue
        if length == 0:
            return ".".join(labels), (end if jumped else pos + 1)
        labels.append(msg[pos + 1:pos + 1 + length].decode("utf8", "replace"))
        pos += 1 + length
    raise ValueError("DNS name too long or looping")


def _records(msg: bytes) -> Iterator[tuple[str, int, int, int]]:
    """Every resource record in a DNS message: (name, type, rdata start, rdata length)."""
    qd, an, ns, ar = struct.unpack("!4H", msg[4:12])
    pos = 12
    for _ in range(qd):
        _, pos = _read_name(msg, pos)
        pos += 4
    for _ in range(an + ns + ar):
        name, pos = _read_name(msg, pos)
        rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", msg[pos:pos + 10])
        pos += 10
        yield name.lower(), rtype, pos, rdlen
        pos += rdlen


def mdns_browse(service: str = MDNS_SERVICE, timeout: float = 3.0,
                iface: IPv4Address | None = None) -> dict[IPv4Address, MdnsService]:
    """
    Browse for a DNS-SD service. Returns {address: MdnsService}.

    The query is sent from an ordinary port, so responders answer by unicast straight to it (RFC 6762 §6.7)
    and nothing competes with the operating system's own mDNS responder for port 5353.
    """
    iface = _iface(iface)
    service = service.lower().rstrip(".")
    query = (struct.pack("!6H", random.randint(1, 0xFFFF), 0, 1, 0, 0, 0) + _encode_name(service)
             + struct.pack("!HH", _PTR, 1))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    if iface:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface.packed)
        s.bind((str(iface), 0))
    s.settimeout(0.3)
    instances: set[str] = set()
    srv: dict[str, tuple[str, int]] = {}
    txt: dict[str, dict[str, str]] = {}
    addrs: dict[str, IPv4Address] = {}
    sources: dict[str, IPv4Address] = {}
    t0 = time.monotonic()
    try:
        s.sendto(query, MDNS_ADDR)
        resent = False
        while time.monotonic() - t0 < timeout:
            if not resent and time.monotonic() - t0 > timeout / 3:
                s.sendto(query, MDNS_ADDR)          # multicast is lossy
                resent = True
            try:
                msg, addr = s.recvfrom(9000)
            except socket.timeout:
                continue
            try:
                for name, rtype, pos, rdlen in _records(msg):
                    if rtype == _PTR and name == service:
                        instance, _ = _read_name(msg, pos)
                        instances.add(instance.lower())
                        sources[instance.lower()] = IPv4Address(addr[0])
                    elif rtype == _SRV:
                        _prio, _weight, port = struct.unpack("!3H", msg[pos:pos + 6])
                        target, _ = _read_name(msg, pos + 6)
                        srv[name] = (target.lower(), port)
                    elif rtype == _TXT:
                        items: dict[str, str] = {}
                        p = pos
                        while p < pos + rdlen:
                            n = msg[p]
                            key, _, value = msg[p + 1:p + 1 + n].decode("utf8", "replace").partition("=")
                            if key:
                                items[key] = value
                            p += 1 + n
                        txt[name] = items
                    elif rtype == _A and rdlen == 4:
                        addrs[name] = IPv4Address(msg[pos:pos + 4])
            except (ValueError, IndexError, struct.error):
                continue                             # not a well-formed DNS message
    finally:
        s.close()

    found: dict[IPv4Address, MdnsService] = {}
    for instance in instances:
        host, port = srv.get(instance, (None, None))
        ip = (addrs.get(host) if host else None) or sources[instance]
        found[ip] = MdnsService(instance, host, port, txt.get(instance, {}))
    return found
