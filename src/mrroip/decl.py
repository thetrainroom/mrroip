# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
The declaration grammar shared by parameters and objects (MRROIP-1.md §7.2, §7.3), and validation of a value
against it. The Rust crate mrroip-proto has the same function (decl.rs); the two must agree.
"""

import math
from typing import TypeGuard

from ._types import Json, JsonObject, as_list, as_object


def key(decl: JsonObject) -> str:
    """`id` for an object, `name` for a parameter."""
    return decl.get("id") or decl.get("name") or ""


def is_number(v: Json) -> TypeGuard[int | float]:
    """A JSON number; booleans are not numbers here, although Python's bool is an int."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check(decl: JsonObject, value: Json) -> str | None:
    """None if the declaration accepts the whole value, otherwise the §8.2 reason."""
    t: str = decl.get("type", "")
    if t.endswith("[]"):
        items = as_list(value)
        if items is None:
            return "wrong_type"
        n = len(items)
        if "count" in decl:
            if n != decl["count"]:
                return "wrong_count"
        elif n < decl.get("min_count", 0) or ("max_count" in decl and n > decl["max_count"]):
            return "wrong_count"
        for item in items:
            reason = _check_element(decl, t[:-2], item)
            if reason:
                return reason
        return None
    return _check_element(decl, t, value)


def _check_element(decl: JsonObject, t: str, value: Json) -> str | None:
    if t in ("int", "float"):
        if not is_number(value) or math.isnan(value):
            return "wrong_type"
        # an infinity is out of range rather than the wrong type, as in the C core (floor(inf) == inf)
        if t == "int" and isinstance(value, float) and math.isfinite(value) and not value.is_integer():
            return "wrong_type"
        if ("min" in decl and value < decl["min"]) or ("max" in decl and value > decl["max"]):
            return "out_of_range"
        return None
    if t == "bool":
        return None if isinstance(value, bool) else "wrong_type"
    if t in ("string", "resource", "enum"):
        if not isinstance(value, str):
            return "wrong_type"
        if t != "enum" and "max_len" in decl and len(value.encode()) > decl["max_len"]:
            return "too_long"
        if "values" in decl and value not in decl["values"]:
            return "not_allowed"
        return None
    if t == "object":
        record = as_object(value)
        if record is None:
            return "wrong_type"
        fields: dict[str, JsonObject] = {key(f): f for f in decl.get("fields", [])}
        for k, v in record.items():
            if k not in fields:
                return "wrong_type"
            reason = check(fields[k], v)
            if reason:
                return reason
        return None
    return None                                 # a type this version does not know


def detail(decl: JsonObject, reason: str) -> JsonObject:
    """The details[] entry for a refusal (§8.2)."""
    d: JsonObject = {"key": key(decl), "reason": reason}
    if reason == "out_of_range":
        d.update({k: decl[k] for k in ("min", "max") if k in decl})
    elif reason == "too_long" and "max_len" in decl:
        d["max_len"] = decl["max_len"]
    elif reason == "not_allowed" and "values" in decl:
        d["values"] = list(decl["values"])
    return d
