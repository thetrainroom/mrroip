"""
Finding MRRoIP endpoints (MRROIP-CORE-SPEC.md §6): SSDP, which is normative, the whois probe, which is a
diagnostic for networks that block multicast, and mDNS, which is a convenience.

    ssdp_search()       ask: every endpoint that answers an M-SEARCH
    NotifyListener      listen: the NOTIFY messages endpoints send by themselves (alive, byebye)
    whois()             ask one endpoint, or broadcast
    mdns_browse()       DNS-SD browse for _mrroip._tcp with a one-shot mDNS query

On a computer with several networks, multicast and broadcast leave through the default interface. Pass
`iface` (a local IPv4 address) to use another one, or set MRROIP_IFACE for code you cannot change, such as
the conformance probe. Standard library only.
"""

import json
import os
import random
import socket
import struct
import threading
import time

from . import protocol

MDNS_ADDR = ("224.0.0.251", 5353)
MDNS_SERVICE = f"{protocol.MDNS_SERVICE}._tcp.local"


def _iface(iface):
    return iface or os.environ.get("MRROIP_IFACE") or None


def parse_ssdp(data):
    """(first line, {HEADER: value}) of an SSDP message."""
    lines = data.decode("utf8", "replace").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().upper()] = v.strip()
    return lines[0], headers


def _is_mrroip(headers):
    return (protocol.TOKEN in headers.get("USN", "").lower()
            or protocol.TOKEN in headers.get("ST", "").lower()
            or protocol.TOKEN in headers.get("NT", "").lower()
            or f"{protocol.HEADER_PREFIX}ID" in headers)


def ssdp_search(timeout=6.0, st=protocol.SSDP_ST, iface=None):
    """Returns {ip: {header: value}} for every MRRoIP endpoint that answers."""
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
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        s.bind((iface, 0))                      # answers come back to this interface
    s.settimeout(1.0)
    found, t0 = {}, time.monotonic()
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
                found[addr[0]] = hdr
    finally:
        s.close()
    return found


class NotifyListener:
    """
    Collects the SSDP NOTIFY messages MRRoIP endpoints send by themselves, in a background thread.

        with NotifyListener() as listener:
            ...                                     # restart a device, rename it, wait
            for e in listener.events:               # {"t": monotonic time, "ip", "nts", "headers"}
                print(e["ip"], e["nts"])
    """

    def __init__(self, iface=None):
        self.iface = _iface(iface)
        self.events = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = None
        self._thread = None

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)   # other SSDP listeners may hold the port
        s.bind(("", protocol.SSDP_ADDR[1]))
        membership = socket.inet_aton(protocol.SSDP_ADDR[0]) + socket.inet_aton(self.iface or "0.0.0.0")
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        s.settimeout(0.2)
        self._sock = s
        self._thread = threading.Thread(target=self._run, name="ssdp-notify", daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            first, hdr = parse_ssdp(data)
            if first.startswith("NOTIFY") and _is_mrroip(hdr):
                with self._lock:
                    self.events.append({"t": time.monotonic(), "ip": addr[0],
                                        "nts": hdr.get("NTS", ""), "headers": hdr})

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._sock:
            self._sock.close()

    def since(self, t, ip=None, nts=None):
        """Events after monotonic time t, optionally for one address and one NTS ("ssdp:alive", "ssdp:byebye")."""
        with self._lock:
            return [e for e in self.events
                    if e["t"] >= t and (ip is None or e["ip"] == ip) and (nts is None or e["nts"] == nts)]

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


def whois(ip=None, timeout=3.0, iface=None):
    """Diagnostic probe. Unicast if ip is given, otherwise broadcast."""
    iface = _iface(iface)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if iface:
        s.bind((iface, 0))
    s.settimeout(0.6)
    target = (ip or "255.255.255.255", protocol.WHOIS_PORT)
    out, t0 = {}, time.monotonic()
    try:
        for _ in range(3):
            s.sendto(json.dumps({"m": "whois"}).encode(), target)
            time.sleep(0.1)
        while time.monotonic() - t0 < timeout:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            try:    out[addr[0]] = json.loads(data)
            except Exception: pass
    finally:
        s.close()
    return out


# ------------------------------------------------------------------ mDNS (RFC 6762 / 6763)

_PTR, _TXT, _SRV, _A = 12, 16, 33, 1


def _encode_name(name):
    return b"".join(bytes([len(label)]) + label.encode() for label in name.rstrip(".").split(".")) + b"\0"


def _read_name(msg, pos):
    """(name, position after it), following compression pointers."""
    labels, jumped, end = [], False, pos
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


def _records(msg):
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


def mdns_browse(service=MDNS_SERVICE, timeout=3.0, iface=None):
    """
    Browse for a DNS-SD service. Returns {ip: {"instance", "host", "port", "txt": {key: value}}}.

    The query is sent from an ordinary port, so responders answer by unicast straight to it (RFC 6762 §6.7)
    and nothing competes with the operating system's own mDNS responder for port 5353.
    """
    iface = _iface(iface)
    service = service.lower().rstrip(".")
    query = struct.pack("!6H", random.randint(1, 0xFFFF), 0, 1, 0, 0, 0) + _encode_name(service) + struct.pack("!HH", _PTR, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    if iface:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        s.bind((iface, 0))
    s.settimeout(0.3)
    instances, srv, txt, addrs, sources = set(), {}, {}, {}, {}
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
                        sources[instance.lower()] = addr[0]
                    elif rtype == _SRV:
                        _prio, _weight, port = struct.unpack("!3H", msg[pos:pos + 6])
                        target, _ = _read_name(msg, pos + 6)
                        srv[name] = (target.lower(), port)
                    elif rtype == _TXT:
                        items, p = {}, pos
                        while p < pos + rdlen:
                            n = msg[p]
                            key, _, value = msg[p + 1:p + 1 + n].decode("utf8", "replace").partition("=")
                            if key:
                                items[key] = value
                            p += 1 + n
                        txt[name] = items
                    elif rtype == _A and rdlen == 4:
                        addrs[name] = socket.inet_ntoa(msg[pos:pos + 4])
            except (ValueError, IndexError, struct.error):
                continue                             # not a well-formed DNS message
    finally:
        s.close()

    found = {}
    for instance in instances:
        host, port = srv.get(instance, (None, None))
        ip = addrs.get(host) or sources.get(instance)
        found[ip] = {"instance": instance, "host": host, "port": port, "txt": txt.get(instance, {})}
    return found
