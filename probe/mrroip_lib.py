# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
mrroip_lib.py — harness for the MRRoIP conformance probe.

Result plumbing and profile hooks. Transport and discovery live in the mrroip package (../src/mrroip);
everything mrroip_probe.py and profile_<device_type>.py modules import from here is still exported under the
same names. Core tests live in mrroip_probe.py, profile tests in profile_<device_type>.py.

Standard library only. Python 3.12+.
"""

import sys
from collections.abc import Callable
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

try:
    import mrroip
except ImportError:                     # not installed: use the package next to this folder
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import mrroip

import time

from mrroip.discovery import ssdp_search, whois
from mrroip.protocol import CORE_MODES, SSDP_ADDR, SSDP_ST, WHOIS_PORT
from mrroip.protocol import TOKEN as PROTO_TOKEN

__all__ = ["Res", "test", "Dev", "Hooks", "ssdp_search", "whois", "wait_back", "ask",
           "PROTO_TOKEN", "SSDP_ST", "SSDP_ADDR", "WHOIS_PORT", "CORE_MODES"]


# ------------------------------------------------------------------ results

class Res:
    """One test outcome. ok is True / False / None (skipped)."""
    def __init__(self, tid: str, name: str) -> None:
        self.id, self.name = tid, name
        self.ok: bool | None = None
        self.note = ""
        self.star = False

    @staticmethod
    def of(tid: str) -> "Res":
        """A result for the registered test tid, under the name it was registered with."""
        return Res(tid, _NAMES[tid])

    def passed(self, note: object = "") -> "Res":
        self.ok, self.note = True, str(note)
        return self

    def failed(self, note: object) -> "Res":
        self.ok, self.note = False, str(note)
        return self

    def skipped(self, note: object) -> "Res":
        self.ok, self.note = None, str(note)
        return self


_NAMES: dict[str, str] = {}


class Test:
    """A registered test: the function, and the id, name and star the report shows."""
    def __init__(self, fn: Callable[..., Res], tid: str, name: str, star: bool) -> None:
        self.fn, self._id, self._name, self._star = fn, tid, name, star

    def __call__(self, *args: Any) -> Res:
        return self.fn(*args)


def test(tid: str, name: str, star: bool = False) -> Callable[[Callable[..., Res]], Test]:
    """Register a test function. tid is 'C-7' or 'P-14'."""
    def deco(fn: Callable[..., Res]) -> Test:
        _NAMES[tid] = name
        return Test(fn, tid, name, star)
    return deco


# ------------------------------------------------------------------ device

class Dev(mrroip.Device):
    """The probe's device. Counts seq from 1000, which C-17 and C-18 rely on when they set seq themselves."""
    def __init__(self, ip: IPv4Address | str, udp_port: int = 5300, timeout: float = 4.0) -> None:
        super().__init__(ip, udp_port, timeout, seq_start=1000)


def wait_back(d: mrroip.Device, limit: float = 45) -> bool:
    """Wait for a device to come back after a power cycle."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        try:
            d.state(); return True
        except Exception:
            time.sleep(1.0)
    return False


def ask(prompt: str) -> str:
    try:
        return input(f"    >>> {prompt} [enter to continue, s to skip] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "s"


# ------------------------------------------------------------------ profile hooks

class Hooks:
    """
    What the core suite needs from a profile in order to test behaviour that is
    generic in principle but needs a device to actually do something: idempotence,
    the class-dependent timeout, and estop.

    A profile module supplies a subclass. Without one, those core tests skip
    rather than guess — a core suite that invents a mode name is testing the
    profile it guessed, not the endpoint in front of it.
    """
    #: a mode that makes the endpoint busy for a while (desired state)
    activate: str | None = None
    #: a mode that returns it to rest
    rest: str = "hold"
    #: dotted path in state.profile to a counter of completed activities
    counter: str | None = None

    def cycle_seconds(self, d: mrroip.Device) -> float:
        """Roughly how long one activity takes, for sizing waits."""
        return 10.0

    def prepare_fast(self, d: mrroip.Device) -> None:
        """Configure the device for short cycles, so tests do not take minutes."""
        return None

    def provoke_fault(self, d: mrroip.Device) -> bool:
        """Put the device into state.fault. Return False if it cannot."""
        return False

    def clear_fault(self, d: mrroip.Device) -> None:
        d.control(mode="reset")
