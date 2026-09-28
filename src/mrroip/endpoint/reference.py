# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
The `reference` profile (conformance/PROFILE-REFERENCE.md): a lamp that is on, off or blinking. Its class is
fixed when the endpoint starts, so one profile exercises every branch of the control timeout (§11.3). The
lamp is a variable; its changes are logged.
"""

import logging
from typing import Final

from .._types import Json, JsonObject
from .params import Param
from .profile import Core, Profile

log = logging.getLogger("mrroip.reference")

LEVEL: Final[JsonObject] = {"id": "level", "type": "int", "min": 0, "max": 100, "default": 100, "unit": "%"}
BLINK_STARTS: Final[JsonObject] = {"id": "blink_starts", "access": "state", "type": "int", "min": 0, "max": 2147483647}


class Reference(Profile):
    device_type = "reference"
    profile_version = "1.0"
    modes = ("off", "on", "blink")
    rest_mode = "off"

    def __init__(self, device_class: str = "passive") -> None:
        if device_class not in ("mobile", "stationary", "passive"):
            raise ValueError(f"device_class {device_class!r}")
        self.device_class = device_class
        self.autonomous = device_class == "stationary"
        self.output = "off"             # what the profile was told: off, on, blink or programme
        self.level = 100
        self.blink_starts = 0
        self.since = 0                  # when blinking or the programme started
        self.cycles = 0                 # programme repetitions counted so far
        self.stopped = False            # estop
        self.faulted: str | None = None
        self.lamp = False
        self._simulated = 0

    def params(self) -> list[Param]:
        return [
            Param("blink_period_ms", "int", 500, min=50, max=5000, unit="ms"),
            Param("programme_blinks", "int", 4, min=1, max=100),
            Param("simulate_fault", "int", 0, min=0, max=1),
        ]

    def start(self, core: Core) -> None:
        super().start(core)
        self._simulated = core.param("simulate_fault")
        if self.autonomous:
            self.resume()               # the programme runs from boot

    def objects(self) -> list[JsonObject]:
        return [dict(LEVEL), dict(BLINK_STARTS)]

    # -- behaviour ---------------------------------------------------------------------------------------

    def _go(self, output: str) -> None:
        now = self.core.now_ms()
        if output in ("blink", "programme") and output != self.output:
            self.blink_starts += 1      # a transition into blinking (§9.1: a repeat adds nothing)
            self.since, self.cycles = now, 0
        self.output = output
        self._update(now)

    def _update(self, now: int) -> None:
        period: int = self.core.param("blink_period_ms")
        if self.stopped or self.faulted or self.output == "off":
            lit = False
        elif self.output == "on":
            lit = True
        elif self.output == "blink":
            lit = ((now - self.since) // (period // 2)) % 2 == 0
        else:                           # programme: blink programme_blinks times, then dark for one period
            blinks: int = self.core.param("programme_blinks")
            cycle = (blinks + 1) * period
            n = (now - self.since) // cycle
            if n > self.cycles:
                self.blink_starts += n - self.cycles
                self.cycles = n
            into = (now - self.since) % cycle
            lit = into < blinks * period and (into // (period // 2)) % 2 == 0
        if lit != self.lamp:
            self.lamp = lit
            log.debug("lamp %s", "on" if lit else "off")

    def tick(self, now_ms: int) -> None:
        self._update(now_ms)

    def param_changed(self, name: str) -> None:
        if name == "simulate_fault":
            value: int = self.core.param("simulate_fault")
            if value == 1 and self._simulated == 0:
                self.faulted = "simulated"
                log.warning("FAULT simulated")
            self._simulated = value
            self._update(self.core.now_ms())

    def state(self) -> tuple[JsonObject, bool, str | None]:
        blinking = self.output in ("blink", "programme") and not self.stopped and not self.faulted
        return ({"lamp": "on" if self.lamp else "off", "level": self.level, "blink_starts": self.blink_starts},
                blinking, self.faulted)

    def fault(self) -> str | None:
        return self.faulted

    def check(self, object_id: str, value: Json) -> str | None:
        if object_id == "blink_starts":
            return "not_commandable"
        if object_id != "level":
            return "unknown_object"
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != int(value):
            return "wrong_type"
        return None if 0 <= value <= 100 else "out_of_range"

    def apply(self, mode: str, objects: JsonObject) -> None:
        if "level" in objects:
            self.level = int(objects["level"])
        self._go(mode)

    def estop(self) -> None:
        self.stopped = True
        self._go("off")

    def reset(self) -> None:
        self.stopped = False
        self.faulted = None
        self._go("off")

    def come_to_rest(self) -> None:
        self._go("off")

    def resume(self) -> None:
        self._go("programme")
