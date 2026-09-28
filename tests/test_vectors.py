# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
Replays conformance/vectors/*.json against the Python endpoint core with the reference profile and a fake
clock (conformance/README.md).

    python3.12 -m unittest discover tests
"""

import json
import logging
import pathlib
import sys
import unittest
from ipaddress import IPv4Address

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mrroip.endpoint.core import Endpoint           # noqa: E402
from mrroip.endpoint.reference import Reference     # noqa: E402

TICK_MS = 10

logging.disable(logging.WARNING)


def match(want, got, path="body"):
    """None if got matches the expectation, else where it differs."""
    if want == "$any":
        return None
    if isinstance(want, dict) and "$contains" in want:
        if not isinstance(got, list):
            return f"{path}: expected a list, got {got!r}"
        for w in want["$contains"]:
            if all(match(w, g) for g in got):
                return f"{path}: no element matches {w!r} in {got!r}"
        return None
    if isinstance(want, dict):
        if not isinstance(got, dict):
            return f"{path}: expected an object, got {got!r}"
        for k, w in want.items():
            if w == "$absent":
                if k in got:
                    return f"{path}.{k}: expected absent, got {got[k]!r}"
                continue
            if k not in got:
                return f"{path}.{k}: missing"
            diff = match(w, got[k], f"{path}.{k}")
            if diff:
                return diff
        return None
    if isinstance(want, list):
        if not isinstance(got, list) or len(got) != len(want):
            return f"{path}: expected {want!r}, got {got!r}"
        for i, (w, g) in enumerate(zip(want, got)):
            diff = match(w, g, f"{path}[{i}]")
            if diff:
                return diff
        return None
    numbers = (int, float)
    if isinstance(want, numbers) and isinstance(got, numbers) and not isinstance(want, bool) and not isinstance(got, bool):
        return None if want == got else f"{path}: expected {want!r}, got {got!r}"
    return None if want == got and type(want) is type(got) else f"{path}: expected {want!r}, got {got!r}"


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def run_case(case):
    clock = Clock()
    ep = Endpoint(Reference(case.get("class", "passive")), "02:00:00:00:00:01", firmware="0.2.0", clock=clock)
    for i, step in enumerate(case["steps"]):
        where = f"step {i + 1}"
        if "advance_ms" in step:
            end = clock.ms + step["advance_ms"]
            while clock.ms < end:
                clock.ms = min(clock.ms + TICK_MS, end)
                ep.tick()
            continue
        peer = IPv4Address(step.get("peer", "10.0.0.1"))
        expect = step.get("expect", {})
        if "udp" in step:
            body = step["udp"]
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            status, got = None, json.loads(ep.udp(data, peer))
        else:
            if "get" in step:
                method, path, body = "GET", step["get"], None
            else:
                method = "POST"
                path = "/config" if "config" in step else "/control"
                body = step.get("config", step.get("control"))
                body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            r = ep.http(method, path, {}, body, peer)
            status, got = r.status, r.body
        if "status" in expect and status != expect["status"]:
            return f"{where}: status {status}, expected {expect['status']}: {got}"
        if "body" in expect:
            diff = match(expect["body"], got)
            if diff:
                return f"{where}: {diff}"
    return None


class Vectors(unittest.TestCase):
    def test_vectors(self):
        for file in sorted((ROOT / "conformance" / "vectors").glob("*.json")):
            for case in json.loads(file.read_text())["cases"]:
                with self.subTest(file=file.name, case=case["name"]):
                    failure = run_case(case)
                    if failure:
                        self.fail(failure)


if __name__ == "__main__":
    unittest.main()
