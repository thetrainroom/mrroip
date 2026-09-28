"""
mrroip_lib.py — harness for the MRRoIP conformance probe.

Result plumbing and profile hooks. Transport and discovery live in the mrroip package (../src/mrroip);
everything mrroip_probe.py and profile_<device_type>.py modules import from here is still exported under the
same names. Core tests live in mrroip_probe.py, profile tests in profile_<device_type>.py.

Standard library only. Python 3.9+.
"""

import sys
from pathlib import Path

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
    def __init__(self, tid, name):
        self.id, self.name, self.ok, self.note = tid, name, None, ""
    def passed(self, note=""):  self.ok, self.note = True, note;  return self
    def failed(self, note):     self.ok, self.note = False, note; return self
    def skipped(self, note):    self.ok, self.note = None, note;  return self


def test(tid, name, star=False):
    """Register a test function. tid is 'C-7' or 'P-14'."""
    def deco(fn):
        fn._id, fn._name, fn._star = tid, name, star
        return fn
    return deco


# ------------------------------------------------------------------ device

class Dev(mrroip.Device):
    """The probe's device. Counts seq from 1000, which C-17 and C-18 rely on when they set seq themselves."""
    def __init__(self, ip, udp_port=5300, timeout=4.0):
        super().__init__(ip, udp_port, timeout, seq_start=1000)


def wait_back(d, limit=45):
    """Wait for a device to come back after a power cycle."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        try:
            d.state(); return True
        except Exception:
            time.sleep(1.0)
    return False


def ask(prompt):
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
    activate = None
    #: a mode that returns it to rest
    rest = "hold"
    #: dotted path in state.profile to a counter of completed activities
    counter = None

    def cycle_seconds(self, d):
        """Roughly how long one activity takes, for sizing waits."""
        return 10.0

    def prepare_fast(self, d):
        """Configure the device for short cycles, so tests do not take minutes."""
        return None

    def provoke_fault(self, d):
        """Put the device into state.fault. Return False if it cannot."""
        return False

    def clear_fault(self, d):
        d.control(mode="reset")
