# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""Type names shared by the package. JSON documents stay dynamic: what an endpoint sends is checked against
its declarations at run time (decl.py), not by the type checker."""

from typing import Any, TypeAlias, cast

#: any JSON value
Json: TypeAlias = Any
#: a JSON object: /definition, /config, /state, a control message or response
JsonObject: TypeAlias = dict[str, Any]


def as_object(value: Json) -> JsonObject | None:
    """value as a JSON object, or None if it is something else."""
    return cast(JsonObject, value) if isinstance(value, dict) else None


def as_list(value: Json) -> list[Json] | None:
    """value as a JSON array, or None if it is something else."""
    return cast(list[Json], value) if isinstance(value, list) else None


#: a rectangle (x, y, w, h) in pixels
Rect: TypeAlias = tuple[int, int, int, int]
