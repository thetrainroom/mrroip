# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
The endpoint core without a network: requests in, responses out (MRROIP-1.md §7–§11). server.py puts it on
sockets; the conformance vectors drive it directly with a fake clock. Everything here runs under one lock,
so a profile needs none of its own.
"""

import json
import math
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Address

from .. import protocol
from .._types import Json, JsonObject
from ..discovery import WhoisReply
from .control import Control
from .params import Params
from .profile import Profile
from .store import MemoryStore, Store


@dataclass
class Response:
    status: int
    body: JsonObject | None = None
    headers: dict[str, str] = field(default_factory=dict[str, str])
    #: close the connection after answering, e.g. with a request body left unread
    close: bool = False

    def encoded(self) -> bytes:
        if self.body is None:
            return b""
        return json.dumps(self.body, separators=(",", ":")).encode()


class Endpoint:
    def __init__(self, profile: Profile, device_id: str, store: Store | None = None, firmware: str = "0.0.0",
                 clock: Callable[[], int] | None = None, http_port: int = 80,
                 on_restart: Callable[[bool], None] | None = None,
                 on_name_changed: Callable[[], None] | None = None) -> None:
        self.profile = profile
        self.device_id = device_id
        self.firmware = firmware
        self.http_port = http_port
        self.store = store or MemoryStore()
        t0 = time.monotonic()
        self._clock: Callable[[], int] = clock or (lambda: int((time.monotonic() - t0) * 1000))
        self.lock = threading.RLock()
        self.params = Params(profile.params(), self.store, profile.device_type)
        self.control = Control(self)
        self.on_restart: Callable[[bool], None] = on_restart or (lambda factory_reset: None)
        self.on_name_changed: Callable[[], None] = on_name_changed or (lambda: None)
        self.last_traffic_ms = self.now_ms()
        #: diagnostic members of /state only (§9.6), e.g. {"network": "ethernet"}
        self.state_extras: JsonObject = {}
        with self.lock:
            profile.start(self)

    # -- what the profile may ask ------------------------------------------------------------------------

    def param(self, name: str) -> Json:
        return self.params.get(name)

    def now_ms(self) -> int:
        return self._clock()

    def master_ip(self) -> IPv4Address | None:
        return self.control.master_ip()

    def note_traffic(self) -> None:
        self.last_traffic_ms = self.now_ms()

    def tick(self) -> None:
        """Call about every 10 ms: the control timeout and whatever the profile does over time."""
        with self.lock:
            now = self.now_ms()
            self.control.tick(now)
            self.profile.tick(now)

    # -- documents ---------------------------------------------------------------------------------------

    def etag(self) -> str:
        # /definition carries device_name and udp_control_port, so a config write must change the tag
        return f'"{self.firmware}-{self.profile.profile_version}-{self.profile.etag()}-{self.params.config_version}"'

    def definition(self) -> JsonObject:
        p = self.profile
        return {
            "proto": protocol.NAME, "proto_version": protocol.VERSION,
            "device_id": self.device_id, "device_name": self.params.get("device_name"),
            "device_type": p.device_type, "device_class": p.device_class,
            "profile_version": p.profile_version, "firmware": self.firmware,
            "endpoints": {"definition": "/definition", "config": "/config", "control": "/control",
                          "state": "/state", "objects": "/objects/{id}",
                          "udp_control_port": self.params.get("udp_port")},
            "capabilities": {"autonomous": bool(p.autonomous), "commanded": True, "telemetry_hz": p.telemetry_hz,
                             "modes": list(p.modes), "core_modes": list(protocol.CORE_MODES)},
            "objects": p.objects(),
            "parameters": self.params.definition(),
        }

    def whois(self, ip: IPv4Address) -> JsonObject:
        """The whois reply (§6.3); ip is the address the asker reached. Off port 80, `definition` is a URL."""
        definition = "/definition" if self.http_port == 80 else f"http://{self.host(ip)}/definition"
        with self.lock:
            name: str = self.params.get("device_name")
        return WhoisReply(self.device_id, name, self.profile.device_type, self.profile.device_class, ip,
                          definition).to_json()

    def host(self, ip: IPv4Address) -> str:
        """How a URL names this endpoint at ip: the address, with the port if it is not 80."""
        return str(ip) if self.http_port == 80 else f"{ip}:{self.http_port}"

    # -- transports --------------------------------------------------------------------------------------

    def udp(self, data: bytes, source_ip: IPv4Address) -> bytes:
        """One control datagram; the reply to send back to the sender (§5.4)."""
        with self.lock:
            if len(data) > protocol.BODY_MAX:
                reply = self.control.reject("body_too_large")
            else:
                _, reply = self.control.apply(data, source_ip)
        return json.dumps(reply, separators=(",", ":")).encode()

    def http(self, method: str, target: str, headers: Mapping[str, str], body: bytes | None,
             source_ip: IPv4Address) -> Response:
        """One HTTP request with a complete body. headers: {lower-case name: value}."""
        path = urllib.parse.urlsplit(target).path
        routes: dict[str, tuple[str, ...]] = {"/definition": ("GET",), "/config": ("GET", "POST"), "/state": ("GET",), "/control": ("POST",)}
        if path.startswith("/objects/"):
            return Response(405, {"error": "method_not_allowed"})   # PUT goes through upload()
        if path not in routes:
            return Response(404, {"error": "not_found"})
        if method not in routes[path]:
            return Response(405, {"error": "method_not_allowed"})
        if body is not None and len(body) > protocol.BODY_MAX:
            if path == "/control":
                with self.lock:
                    return Response(413, self.control.reject("body_too_large"), close=True)
            return Response(413, {"error": "body_too_large"}, close=True)

        with self.lock:
            if path == "/definition":
                tag = self.etag()
                if headers.get("if-none-match") == tag:
                    return Response(304, None, {"ETag": tag})
                return Response(200, self.definition(), {"ETag": tag})
            if path == "/state":
                return Response(200, {**self.control.state(), **self.state_extras})
            if path == "/control":
                status, reply = self.control.apply(body or b"", source_ip)
                return Response(status, reply)
            if method == "GET":
                return Response(200, self.params.config_json(self.device_id))
            self.note_traffic()
            reply = self.params.apply(body or b"", self.device_id)
            for name in reply.changed:
                if name == "device_name":
                    self.on_name_changed()          # re-announce SSDP and re-register mDNS (§8.2)
                else:
                    self.profile.param_changed(name)
        if reply.restart or reply.factory_reset:
            self.on_restart(reply.factory_reset)    # after the response is sent
        return Response(reply.status, reply.body)

    def upload(self, target: str, headers: Mapping[str, str], length: int, source_ip: IPv4Address,
               read: Callable[[int], bytes]) -> Response:
        """PUT /objects/<id> (§9.8). read(n) returns up to n bytes of the body, b"" at its end."""
        split = urllib.parse.urlsplit(target)
        object_id = split.path[len("/objects/"):]
        if not object_id or len(object_id) >= 32 or "/" in object_id:
            return Response(404, {"error": "not_found"}, close=True)
        request: JsonObject = {}
        for k, v in urllib.parse.parse_qsl(split.query):
            request[k] = int(v) if v.lstrip("-").isdigit() else v
        base = headers.get(f"{protocol.HEADER_PREFIX}base".lower())
        if base is not None:
            request["base"] = base
        seq: int | float | None = None
        try:
            number = float(headers.get(f"{protocol.HEADER_PREFIX}seq".lower(), ""))
            if math.isfinite(number):
                seq = int(number) if number.is_integer() else number
        except ValueError:
            pass
        with self.lock:
            refused = self.control.stream_begin(object_id, request, seq, length, source_ip)
        if refused:
            status, reply = refused
            return Response(status, reply, close=length > 0)    # the unread body is still on the socket
        received, reading = 0, True
        while reading and received < length:
            chunk = read(min(1460, length - received))
            if not chunk:
                break
            received += len(chunk)
            with self.lock:
                reading = self.profile.stream_data(chunk)
        complete = received == length
        with self.lock:
            self.profile.stream_end(complete)
            status, reply = self.control.stream_end(complete, seq)
        return Response(status, reply, close=not complete)
