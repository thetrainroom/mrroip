# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
Replays conformance/grammar.json against mrroip.decl.check. The Rust crate mrroip-proto replays the same file, so
the two checks refuse the same values for the same reasons.

    python3.12 -m unittest discover tests
"""

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mrroip import decl  # noqa: E402


class Grammar(unittest.TestCase):
    def test_cases(self) -> None:
        for case in json.loads((ROOT / "conformance" / "grammar.json").read_text())["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(decl.check(case["decl"], case["value"]), case["reason"])

    def test_non_finite_numbers(self) -> None:
        # not in grammar.json: JSON has no such numbers, but Python's parser makes one of 1e999
        level = {"type": "int", "min": 0, "max": 100}
        self.assertEqual(decl.check(level, float("inf")), "out_of_range")
        self.assertEqual(decl.check(level, float("nan")), "wrong_type")


if __name__ == "__main__":
    unittest.main()
