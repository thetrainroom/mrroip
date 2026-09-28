# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
An endpoint on the network (MRROIP-1.md §5, §6): HTTP, UDP control, SSDP, whois and mDNS around the core.
Discovery follows components/mrroip/src/discovery.c: three start-up announcements 100 ms apart, then one
every announce_interval_s (at least 30), silence at 0 — M-SEARCH answers included — and a return to 300 s
after 30 minutes without /control or /config traffic.

A host has one address that matters here: the one facing the layout. Pass `iface` where there are several.
"""

import http.server
import json
import logging
import os
import random
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ipaddress import IPv4Address
from typing import Final, Self

from .. import protocol
from .core import Endpoint, Response

log = logging.getLogger("mrroip.server")

SSDP_GROUP: Final = IPv4Address(protocol.SSDP_ADDR[0])
SSDP_PORT: Final = protocol.SSDP_ADDR[1]
ANNOUNCE_MIN_S: Final = 30
ANNOUNCE_DEFAULT_S: Final = 300
RECOVERY_MS: Final = 30 * 60 * 1000
STARTUP_REPEATS: Final = 3
STARTUP_GAP_S: Final = 0.1
MAX_AGE_S: Final = 600

#: an SSDP searcher: its address and port
Peer = tuple[IPv4Address, int]
#: make_endpoint(on_restart, on_name_changed)
EndpointFactory = Callable[[Callable[[bool], None], Callable[[], None]], Endpoint]


def ip_facing(peer: IPv4Address) -> IPv4Address | None:
    """The local address a packet to peer leaves from. No packet is sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((str(peer), 9))
        return IPv4Address(s.getsockname()[0])
    except OSError:
        return None
    finally:
        s.close()


def platform_token() -> str:
    u = os.uname()
    return f"{u.sysname.lower()}/{u.release}"


class _HTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _handler(endpoint: Endpoint) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"               # keep-alive (§2.3)
        server_version = f"{protocol.NAME}/{protocol.VERSION}"

        def log_message(self, format: str, *args: object) -> None:
            log.debug("%s %s", self.client_address[0], format % args)

        def _send(self, r: Response) -> None:
            data = r.encoded()
            self.send_response(r.status)
            if r.body is not None:
                self.send_header("Content-Type", "application/json")
            for k, v in r.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            if r.close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _headers(self) -> dict[str, str]:
            return {k.lower(): v for k, v in self.headers.items()}

        def _length(self) -> int:
            try:
                return max(0, int(self.headers.get("Content-Length", "0")))
            except ValueError:
                return 0

        def _peer(self) -> IPv4Address:
            return IPv4Address(self.client_address[0])

        def _handle(self) -> None:
            length = self._length()
            if self.command == "PUT" and self.path.startswith("/objects/"):
                r = endpoint.upload(self.path, self._headers(), length, self._peer(), self.rfile.read)
            elif length > protocol.BODY_MAX:
                # refused unread: the core sees a body one byte too long, and the connection closes after the answer
                r = endpoint.http(self.command, self.path, self._headers(), bytes(protocol.BODY_MAX + 1), self._peer())
            else:
                body = self.rfile.read(length) if length else None
                r = endpoint.http(self.command, self.path, self._headers(), body, self._peer())
            self._send(r)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

    return Handler


class Server:
    """Runs an Endpoint on the network until stop()."""

    def __init__(self, make_endpoint: EndpointFactory, http_port: int = 80, iface: IPv4Address | None = None,
                 mdns: bool = True) -> None:
        """make_endpoint(on_restart, on_name_changed) returns the Endpoint to serve."""
        self.iface = iface
        self.http_port = http_port
        self.mdns = mdns
        self._stop = threading.Event()
        self._name_changed = threading.Event()
        self._mdns_procs: list[subprocess.Popen[bytes]] = []
        self.endpoint = make_endpoint(self._restart, self._name_changed.set)
        self.endpoint.state_extras = {"network": "ethernet"}

    # -- lifecycle ---------------------------------------------------------------------------------------

    def start(self) -> Self:
        self.http = _HTTPServer(("", self.http_port), _handler(self.endpoint))
        self.http_port = self.http.server_address[1]
        self.endpoint.http_port = self.http_port
        threads: list[tuple[str, Callable[[], None]]] = [("http", self.http.serve_forever), ("udp", self._udp), ("tick", self._tick),
                   ("discovery", self._discovery)]
        for name, target in threads:
            threading.Thread(target=target, name=name, daemon=True).start()
        log.info("%s %s (%s) on http port %d, udp %d", self.endpoint.profile.device_type,
                 self.endpoint.device_id, self.endpoint.profile.device_class, self.http_port,
                 self.endpoint.param("udp_port"))
        return self

    def stop(self, byebye: bool = True) -> None:
        if byebye:
            self._byebye()
        self._stop.set()
        self.http.shutdown()
        self._mdns_stop()

    def _restart(self, factory_reset: bool) -> None:
        """After the response is sent: store, say goodbye, start over (§8.2)."""
        def later() -> None:
            time.sleep(0.5)
            if factory_reset:
                self.endpoint.store.erase_all()
            self.endpoint.store.flush()
            self._byebye()
            self._mdns_stop()
            log.warning("restarting%s", " after a factory reset" if factory_reset else "")
            os.execv(sys.executable, sys.orig_argv)
        threading.Thread(target=later, name="restart", daemon=True).start()

    # -- control ------------------------------------------------------------------------------------------

    def _tick(self) -> None:
        while not self._stop.wait(0.01):
            self.endpoint.tick()

    def _udp(self) -> None:
        sock: socket.socket | None = None
        port: int | None = None
        while not self._stop.is_set():
            wanted: int = self.endpoint.param("udp_port")
            if sock is None or wanted != port:                       # a changed udp_port applies within a second
                if sock:
                    sock.close()
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind(("", wanted))
                sock.settimeout(1.0)
                port = wanted
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                port = None
                continue
            sock.sendto(self.endpoint.udp(data, IPv4Address(peer[0])), peer)

    # -- discovery ----------------------------------------------------------------------------------------

    def _address(self, peer: IPv4Address | None = None) -> IPv4Address:
        """This endpoint's address as peer (or the SSDP group) sees it."""
        return self.iface or ip_facing(peer or SSDP_GROUP) or IPv4Address("127.0.0.1")

    def _message(self, kind: str, ip: IPv4Address) -> bytes:
        e = self.endpoint
        with e.lock:
            name: str = e.param("device_name")
        name = "".join("?" if ord(c) < 0x20 else c for c in name)
        usn = f"uuid:{protocol.TOKEN}-{e.device_id.replace(':', '')}::{protocol.SSDP_ST}"
        if kind == "byebye":
            return (f"NOTIFY * HTTP/1.1\r\nHOST: {SSDP_GROUP}:{SSDP_PORT}\r\nNT: {protocol.SSDP_ST}\r\n"
                    f"NTS: ssdp:byebye\r\nUSN: {usn}\r\n\r\n").encode()
        start = (f"NOTIFY * HTTP/1.1\r\nHOST: {SSDP_GROUP}:{SSDP_PORT}\r\n" if kind == "alive"
                 else "HTTP/1.1 200 OK\r\nEXT:\r\n")
        target = f"NT: {protocol.SSDP_ST}\r\nNTS: ssdp:alive\r\n" if kind == "alive" else f"ST: {protocol.SSDP_ST}\r\n"
        p = protocol.HEADER_PREFIX
        return (f"{start}CACHE-CONTROL: max-age={MAX_AGE_S}\r\nLOCATION: http://{e.host(ip)}/definition\r\n"
                f"{target}USN: {usn}\r\nSERVER: {platform_token()} {protocol.NAME}/{protocol.VERSION}\r\n"
                f"{p}ID: {e.device_id}\r\n{p}NAME: {name}\r\n{p}TYPE: {e.profile.device_type}\r\n"
                f"{p}CLASS: {e.profile.device_class}\r\n\r\n").encode()

    def _multicast(self, kind: str, repeats: int = 1) -> None:
        ip = self._address()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, ip.packed)
            for i in range(repeats):
                if i:
                    time.sleep(STARTUP_GAP_S)
                s.sendto(self._message(kind, ip), (str(SSDP_GROUP), SSDP_PORT))
        except OSError as e:
            log.warning("SSDP %s not sent: %s", kind, e)
        finally:
            s.close()

    def _byebye(self) -> None:
        self._multicast("byebye")

    def _interval_s(self) -> int:
        configured: int = self.endpoint.param("announce_interval_s")
        if configured > 0:
            return max(configured, ANNOUNCE_MIN_S)
        quiet = self.endpoint.now_ms() - self.endpoint.last_traffic_ms
        return ANNOUNCE_DEFAULT_S if quiet >= RECOVERY_MS else 0

    def _discovery(self) -> None:
        ssdp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        ssdp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            ssdp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)     # the system may run SSDP too
        ssdp.bind(("", SSDP_PORT))
        membership = SSDP_GROUP.packed + self._address().packed
        ssdp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        whois = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        whois.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        whois.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        whois.bind(("", protocol.WHOIS_PORT))

        self._mdns_start()
        pending: dict[Peer, float] = {}             # searcher -> when to answer
        last_interval, next_announce = -1, 0.0
        while not self._stop.is_set():
            interval = self._interval_s()
            if interval != last_interval:
                if last_interval > 0 and interval == 0:
                    log.warning("SSDP silenced (announce_interval_s = 0)")
                elif last_interval == 0 and interval > 0:
                    log.warning("SSDP announcing again, every %d s", interval)
                if last_interval == -1 and interval > 0:
                    self._multicast("alive", STARTUP_REPEATS)
                    next_announce = time.monotonic() + interval
                else:
                    next_announce = 0 if last_interval == 0 else time.monotonic() + interval
                last_interval = interval

            ready, _, _ = select.select([ssdp, whois], [], [], 0.1)
            if ssdp in ready:
                data, (peer_ip, peer_port) = ssdp.recvfrom(4096)
                peer: Peer = (IPv4Address(peer_ip), peer_port)
                text = data.decode("utf8", "replace")
                # Silenced means silent: mrroip_probe.py C-28 counts an M-SEARCH answer as an announcement
                if interval and text.startswith("M-SEARCH * HTTP/1.1\r\n") and peer not in pending:
                    headers: dict[str, str] = {}
                    for line in text.split("\r\n")[1:]:
                        k, _, v = line.partition(":")
                        headers[k.strip().upper()] = v.strip()
                    st = headers.get("ST", "")
                    if "ssdp:discover" in headers.get("MAN", "") and st in ("ssdp:all", protocol.SSDP_ST):
                        try:
                            mx = min(5, max(1, int(headers.get("MX", "1"))))
                        except ValueError:
                            mx = 1
                        pending[peer] = time.monotonic() + random.uniform(0, mx)
            if whois in ready:
                data, asker = whois.recvfrom(2048)
                if b"whois" in data:
                    reply = self.endpoint.whois(self._address(IPv4Address(asker[0])))
                    whois.sendto(json.dumps(reply).encode(), asker)

            now = time.monotonic()
            for (ip, port), due in list(pending.items()):
                if now >= due:
                    del pending[(ip, port)]
                    ssdp.sendto(self._message("response", self._address(ip)), (str(ip), port))
            if self._name_changed.is_set():
                self._name_changed.clear()
                self._mdns_start()
                next_announce = 0                   # re-announce under the new name
            if interval > 0 and now >= next_announce:
                self._multicast("alive", STARTUP_REPEATS if next_announce == 0 else 1)
                next_announce = now + interval
        ssdp.close()
        whois.close()

    # -- mDNS, through the system's responder (§6.2) --------------------------------------------------------

    def _mdns_start(self) -> None:
        """_mrroip._tcp with its TXT records, and <device_name>.local for this endpoint's address. The host
        name is the endpoint's own, not the computer's, so it is published as a proxy record."""
        self._mdns_stop()
        if not self.mdns:
            return
        e = self.endpoint
        with e.lock:
            name: str = e.param("device_name")
        host = hostname_from(name, e.profile.device_type) + ".local"
        ip = self._address()
        txt = [f"id={e.device_id}", f"name={name}", f"type={e.profile.device_type}",
               f"class={e.profile.device_class}", f"fw={e.firmware}"]
        service = f"{protocol.MDNS_SERVICE}._tcp"
        port = str(self.http_port)
        if shutil.which("avahi-publish"):
            cmds = [["avahi-publish", "-a", "-R", host, str(ip)],
                    ["avahi-publish", "-s", "-H", host, name, service, port, *txt]]
        elif shutil.which("dns-sd"):
            cmds = [["dns-sd", "-P", name, service, "local", port, host, str(ip), *txt]]
        else:
            log.info("no mDNS responder command found; mDNS is a convenience (§6.2), carrying on without it")
            self.mdns = False
            return
        self._mdns_procs = [subprocess.Popen(c, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for c in cmds]

    def _mdns_stop(self) -> None:
        for proc in self._mdns_procs:
            proc.terminate()
        self._mdns_procs = []


def hostname_from(name: str, fallback: str) -> str:
    """A DNS label from a device name, as the C core makes it: lower case, runs of other characters as '-'."""
    out: list[str] = []
    for c in name.lower():
        if c.isascii() and c.isalnum():
            out.append(c)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-") or fallback
