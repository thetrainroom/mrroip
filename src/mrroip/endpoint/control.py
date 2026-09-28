# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
The one control parser (MRROIP-1.md §9, §10, §11, §13 item 1): HTTP and UDP both call Control.apply(). The
Python form of components/mrroip/src/control.c — the same checks in the same order, so that the same
message gets the same error from either implementation.
"""

import json
import logging
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Final

from .. import protocol
from .._types import Json, JsonObject, as_object
from ..decl import is_number

if TYPE_CHECKING:
    from .core import Endpoint

log = logging.getLogger("mrroip.control")

MASTERS_MAX: Final = 8
OBJECTS_MAX: Final = 8


@dataclass(frozen=True)
class Accepted:
    """A sender's last accepted message, for the replay check (§9.5)."""
    seq: int | float
    at_ms: int


class Control:
    def __init__(self, core: "Endpoint") -> None:
        self.core = core
        self.profile = core.profile
        #: the 8 senders accepted most recently
        self.masters: dict[IPv4Address, Accepted] = {}
        self.authority = self._rest_authority()
        self.authority_ip: IPv4Address | None = None
        self.authority_ms = 0
        self.mode = self.profile.rest_mode
        self.estop_latched = False

    def _rest_authority(self) -> str:
        return "autonomous" if self.profile.autonomous else "idle"

    # -- state -------------------------------------------------------------------------------------------

    def state(self) -> JsonObject:
        """The state object of §9.4. Call with the core lock held."""
        profile, busy, fault = self.profile.state()
        return {"mode": self.mode, "authority": self.authority, "busy": busy, "fault": fault,
                "uptime_ms": self.core.now_ms(), "profile": profile}

    def master_ip(self) -> IPv4Address | None:
        return self.authority_ip if self.authority == "commanded" else None

    # -- authority ---------------------------------------------------------------------------------------

    def _master_lost(self, why: str) -> None:
        """Timeout or release: dispatch on device_class (§11.3)."""
        nxt = self._rest_authority()
        if nxt == "autonomous":
            self.profile.resume()                       # stationary with a programme: resume it
        elif self.profile.device_class != "passive":
            self.profile.come_to_rest()                 # mobile stops; stationary without one comes to rest
            if not self.estop_latched:
                self.mode = self.profile.rest_mode
        log.warning("AUTHORITY %s -> %s (%s, master %s)", self.authority, nxt, why, self.authority_ip)
        self.authority = nxt

    def tick(self, now: int) -> None:
        """Arms the timeout only while commanded; /state polling never refreshes it."""
        if self.authority == "commanded" and now - self.authority_ms > self.core.params.get("control_timeout_ms"):
            self._master_lost("timeout")

    def _command(self, source_ip: IPv4Address, now: int, reply: JsonObject) -> None:
        if self.authority == "commanded" and self.authority_ip != source_ip:
            reply["authority_taken_from"] = str(self.authority_ip)    # accepted, but visible (§11.2)
        if self.authority != "commanded" or self.authority_ip != source_ip:
            log.warning("AUTHORITY %s -> commanded (master %s)", self.authority, source_ip)
        self.authority, self.authority_ip, self.authority_ms = "commanded", source_ip, now

    def _stale(self, source_ip: IPv4Address, seq: int | float, now: int) -> bool:
        last = self.masters.get(source_ip)
        return last is not None and seq <= last.seq and now - last.at_ms <= protocol.REPLAY_WINDOW_MS

    def _accept_seq(self, source_ip: IPv4Address, seq: int | float, now: int) -> None:
        self.masters.pop(source_ip, None)
        self.masters[source_ip] = Accepted(seq, now)
        while len(self.masters) > MASTERS_MAX:
            oldest = min(self.masters, key=lambda ip: self.masters[ip].at_ms)
            del self.masters[oldest]

    # -- messages ----------------------------------------------------------------------------------------

    def reject(self, error: str) -> JsonObject:
        """A refusal before parsing, e.g. body_too_large. Call with the core lock held."""
        return {"device_id": self.core.device_id, "accepted": False, "error": error, "state": self.state()}

    def apply(self, body: bytes, source_ip: IPv4Address) -> tuple[int, JsonObject]:
        """One control message from source_ip. Returns (status, reply). Call with the core lock held."""
        self.core.note_traffic()
        info = self.profile
        reply: JsonObject = {"device_id": self.core.device_id}
        status = 400
        missing: str | None = None
        details: list[JsonObject] = []
        try:
            msg: Json = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            msg = None

        def reject(error: str) -> tuple[int, JsonObject]:
            reply["accepted"] = False
            reply["error"] = error
            if missing:
                reply["field"] = missing
            if details:
                reply["details"] = details
            reply["state"] = self.state()
            return status, reply

        message = as_object(msg)
        if message is None:
            return reject("malformed_json")
        seq, ts = message.get("seq"), message.get("ts")
        if is_number(seq):
            reply["ack_seq"] = seq
        if is_number(ts):
            reply["ts"] = ts
        if not is_number(seq):
            missing = "seq"
            return reject("missing_field")
        if "mode" not in message:
            missing = "mode"
            return reject("missing_field")
        requested = message["mode"] if isinstance(message["mode"], str) else ""
        is_core = requested in protocol.CORE_MODES
        is_profile = requested in info.modes
        if not is_core and not is_profile:
            return reject("unknown_mode")
        takes_target = requested in info.target_modes
        if "target" in message and not takes_target:
            return reject("unknown_target")
        if "target" not in message and takes_target:
            return reject("missing_target")

        is_estop = requested == "estop"
        now = self.core.now_ms()
        if not is_estop and self._stale(source_ip, seq, now):
            status = 409                    # a reordered duplicate; after a longer gap the master restarted
            return reject("stale_seq")

        # Objects are desired states that go with a profile mode; check all of them before applying any
        objects = message.get("objects")
        checked: JsonObject = {}
        if objects is not None:
            desired = as_object(objects)
            if not is_profile or desired is None:
                return reject("invalid_object_state" if is_profile else "unexpected_objects")
            for object_id, value in desired.items():
                reason = info.check(object_id, value) if len(checked) < OBJECTS_MAX else "too_many_objects"
                if reason:
                    details.append({"key": object_id, "reason": reason})
                else:
                    checked[object_id] = value
            if details:
                return reject("invalid_object_state")

        hold = message.get("hold") is True
        changes_state = is_profile and not hold
        if changes_state and self.estop_latched:
            status = 409
            return reject("latched_estop")
        if changes_state and info.fault():
            status = 409
            return reject("latched_fault")

        # Accepted
        self._accept_seq(source_ip, seq, now)
        if requested == "release":
            if self.authority == "commanded" and self.authority_ip != source_ip:
                reply["authority_taken_from"] = str(self.authority_ip)
            if self.authority == "commanded":
                self.authority_ip = source_ip
                self._master_lost("release")
        else:
            self._command(source_ip, now, reply)
            if is_estop:
                if not self.estop_latched:
                    info.estop()
                    self.estop_latched = True
                    log.warning("ESTOP latched")
                self.mode = "estop"
            elif requested == "reset":
                info.reset()
                if self.estop_latched:
                    log.warning("ESTOP cleared by reset")
                self.estop_latched = False
                self.mode = info.rest_mode
            elif changes_state:
                info.apply(requested, checked)      # desired state: repeating it changes nothing
                self.mode = requested
            # "hold", or hold: true: authority refreshed, nothing else changes
        reply["accepted"] = True
        reply["state"] = self.state()
        return 200, reply

    # -- binary uploads (§9.8) ---------------------------------------------------------------------------

    def stream_begin(self, object_id: str, request: JsonObject, seq: int | float | None, length: int,
                     source_ip: IPv4Address) -> tuple[int, JsonObject] | None:
        """None if the upload may start, else (status, reply). Call with the core lock held."""
        self.core.note_traffic()
        reply: JsonObject = {"device_id": self.core.device_id}
        if seq is not None:
            reply["ack_seq"] = seq
        now = self.core.now_ms()
        error: str | None = None
        reason: str | None = None
        status = 400
        if seq is None:
            error = "missing_field"
        elif self._stale(source_ip, seq, now):
            error, status = "stale_seq", 409
        elif self.estop_latched:            # the latches before the profile: starting an upload prepares buffers
            error, status = "latched_estop", 409
        elif self.profile.fault():
            error, status = "latched_fault", 409
        else:
            reason = self.profile.stream_begin(object_id, request, length)
            if reason:
                error = "invalid_object_state"
        if error or seq is None:
            reply.update({"accepted": False, "error": error})
            if seq is None:
                reply["field"] = "seq"
            if reason:
                reply["details"] = [{"key": object_id, "reason": reason}]
            reply["state"] = self.state()
            return status, reply
        self._accept_seq(source_ip, seq, now)
        self._command(source_ip, now, {})
        return None

    def stream_end(self, complete: bool, seq: int | float | None) -> tuple[int, JsonObject]:
        """Call with the core lock held, after the profile's stream_end."""
        reply: JsonObject = {"device_id": self.core.device_id}
        if seq is not None:
            reply["ack_seq"] = seq
        reply["accepted"] = complete
        if not complete:
            reply["error"] = "incomplete_body"
        reply["state"] = self.state()
        return (200 if complete else 400), reply
