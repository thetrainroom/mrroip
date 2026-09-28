# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
The interface between the endpoint core and a device profile (MRROIP-1.md §13, §14): the Python form of
components/mrroip/include/mrroip_profile.h. The core never names a profile parameter or object; the profile
never parses JSON or touches a socket.

The core calls every method with its lock held, from whichever thread handled the message, except tick(),
which it calls from its own timer thread — also with the lock held. A profile therefore needs no locking of
its own.
"""

from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Protocol

from .._types import Json, JsonObject

if TYPE_CHECKING:
    from .params import Param


class Core(Protocol):
    """What a profile may ask the core."""

    def param(self, name: str) -> Json:
        """The running value of a parameter."""
        ...

    def now_ms(self) -> int:
        """Milliseconds since the endpoint started; the clock every time in the core is measured on."""
        ...

    def master_ip(self) -> IPv4Address | None:
        """The master holding authority, if one does."""
        ...


class Profile:
    #: identity (§5.1, §14 items 1–2)
    device_type: str = "null"
    device_class: str = "passive"       # "mobile", "stationary" or "passive" (§4.2)
    profile_version: str = "0.1"
    #: profile modes (§9.2), never a core one; modes that require a target
    modes: tuple[str, ...] = ()
    target_modes: tuple[str, ...] = ()
    #: the mode reported at start and after reset (§14 item 5)
    rest_mode: str = ""
    autonomous: bool = False
    telemetry_hz: float = 0

    core: Core

    def params(self) -> "list[Param]":
        """Parameters appended to the core's four (§7.2)."""
        return []

    def start(self, core: Core) -> None:
        """Parameters are loaded by now."""
        self.core = core

    def param_changed(self, name: str) -> None:
        """A profile parameter that applies immediately has a new running value."""

    def objects(self) -> list[JsonObject]:
        """Declarations for /definition "objects" (§7.3)."""
        return []

    def state(self) -> tuple[JsonObject, bool, str | None]:
        """(state.profile members, busy, fault or None)."""
        return {}, False, None

    def fault(self) -> str | None:
        return None

    def etag(self) -> str:
        """The part of the /definition ETag that depends on how this start is configured."""
        return self.profile_version

    def check(self, object_id: str, value: Json) -> str | None:
        """None if the desired state is acceptable, else a reason for details[]. Must not apply anything."""
        return "unknown_object"

    def apply(self, mode: str, objects: JsonObject) -> None:
        """Apply a profile mode and object states already checked. Idempotent (§9.1)."""

    def estop(self) -> None:
        """Halt immediately; the core latches it."""

    def reset(self) -> None:
        """Back to the rest state."""

    def come_to_rest(self) -> None:
        """Loss of the master for a mobile, or stationary without a programme, endpoint (§11.3)."""

    def resume(self) -> None:
        """Loss of the master for an endpoint with an autonomous programme: resume it (§11.3)."""

    def tick(self, now_ms: int) -> None:
        """Called about every 10 ms, for whatever the profile does over time."""

    # Binary object states, PUT /objects/<id> (§9.8). A profile without them keeps these defaults.
    def stream_begin(self, object_id: str, request: JsonObject, length: int) -> str | None:
        return "not_uploadable"

    def stream_data(self, data: bytes) -> bool:
        return False

    def stream_end(self, complete: bool) -> None:
        pass
