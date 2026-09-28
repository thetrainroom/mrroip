# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
The one parameter table (MRROIP-1.md §7, §7.2, §8): /definition is generated from it and /config writes are
validated against it. The Python form of components/mrroip/src/params.c, and it behaves the same way:
three copies of every value — running, stored, and as booted — and writes that apply at once and are
stored only when the write says `persist`.
"""

import copy
import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from .. import decl as grammar
from .. import protocol
from .._types import Json, JsonObject, as_object, parse_json
from .store import Store

CONFIG_VERSION_KEY: Final = "cfg_ver"


@dataclass(frozen=True)
class Param:
    """One parameter: its declaration in the grammar of §7.2, and how the endpoint keeps it."""
    name: str
    type: str                                   # "int", "float", "string", "bool", "enum", "int[]", …
    default: Json = None
    min: int | float | None = None
    max: int | float | None = None
    max_len: int | None = None
    values: tuple[str, ...] | None = None
    unit: str | None = None
    doc: str | None = None
    #: accepted only with persist; the endpoint restarts to apply it (§7.2)
    applies_at_restart: bool = False
    #: the key in the store, where it differs from the name (NVS keys have at most 15 characters)
    store_key: str | None = None
    #: further keys of the grammar: count, min_count, max_count, fields
    extra: JsonObject = field(default_factory=dict[str, Json])

    @property
    def key(self) -> str:
        return self.store_key or self.name

    def decl(self) -> JsonObject:
        """The declaration as /definition shows it, and as a write is checked against."""
        out: JsonObject = {"name": self.name, "type": self.type, "default": copy.deepcopy(self.default)}
        for k in ("min", "max", "max_len", "unit"):
            value = getattr(self, k)
            if value is not None:
                out[k] = value
        if self.values is not None:
            out["values"] = list(self.values)
        out.update(copy.deepcopy(self.extra))
        out["persist"] = True
        if self.applies_at_restart:
            out["applies"] = "restart"
        if self.doc:
            out["doc"] = self.doc
        return out


CORE_PARAMS: Final = (
    Param("device_name", "string", max_len=31, doc="Human label. Not an identifier."),
    Param("control_timeout_ms", "int", 2000, min=500, max=30000, unit="ms", store_key="ctrl_timeout_ms"),
    Param("udp_port", "int", protocol.UDP_PORT, min=1024, max=65535),
    Param("announce_interval_s", "int", 300, min=0, max=86400, unit="s", store_key="announce_int_s",
          doc="0 disables SSDP announcements; see rate-limit recovery."),
)


@dataclass
class ConfigReply:
    status: int
    body: JsonObject
    #: restart once the response is sent
    restart: bool = False
    #: erase the store, then restart, once the response is sent
    factory_reset: bool = False
    #: parameters whose running value changed and that apply at once
    changed: list[str] = field(default_factory=list[str])


class Params:
    def __init__(self, profile_params: Sequence[Param], store: Store, device_type: str) -> None:
        core = list(CORE_PARAMS)
        core[0] = dataclasses.replace(core[0], default=device_type)     # device_name defaults to the device type
        self.table: list[Param] = core + list(profile_params)
        self.by_name: dict[str, Param] = {p.name: p for p in self.table}
        self._decls: dict[str, JsonObject] = {p.name: p.decl() for p in self.table}
        self.store = store
        stored_values = store.load()
        self.stored: JsonObject = {}
        for p in self.table:
            value = stored_values.get(p.key, p.default)
            if grammar.check(self._decls[p.name], value) is not None:
                value = p.default                       # a stored value that is not valid any more
            self.stored[p.name] = value
        self.running = dict(self.stored)
        self.booted = dict(self.stored)
        self.config_version = int(stored_values.get(CONFIG_VERSION_KEY, 0))

    def get(self, name: str) -> Json:
        return self.running[name]

    def definition(self) -> list[JsonObject]:
        """Entries of /definition "parameters"."""
        return [copy.deepcopy(self._decls[p.name]) for p in self.table]

    def config_json(self, device_id: str) -> JsonObject:
        """Body of GET /config (§8.1)."""
        dirty = [p.name for p in self.table if self.running[p.name] != self.stored[p.name]]
        restart = [p.name for p in self.table
                   if p.applies_at_restart and self.running[p.name] != self.booted[p.name]]
        return {"device_id": device_id,
                "config": {p.name: copy.deepcopy(self.running[p.name]) for p in self.table},
                "_meta": {"dirty": bool(dirty), "dirty_keys": dirty, "config_version": self.config_version,
                          "restart_pending_keys": restart}}

    def apply(self, body: bytes, device_id: str) -> ConfigReply:
        """POST /config (§8.2): validate every key first, then apply all of them or none."""
        try:
            root = as_object(parse_json(body))
        except (ValueError, UnicodeDecodeError):
            root = None
        if root is None:
            return ConfigReply(400, {"error": "malformed_json"})

        details: list[JsonObject] = []
        pending: JsonObject = {}
        persist = factory_reset = False
        if_version: int | float | None = None
        for k, v in root.items():
            if k in ("persist", "factory_reset"):
                if not isinstance(v, bool):
                    details.append({"key": k, "reason": "wrong_type"})
                elif k == "persist":
                    persist = v
                else:
                    factory_reset = v
                continue
            if k == "if_version":
                if not grammar.is_number(v):
                    details.append({"key": k, "reason": "wrong_type"})
                else:
                    if_version = v
                continue
            p = self.by_name.get(k)
            if p is None:
                details.append({"key": k, "reason": "unknown_key"})
                continue
            reason = grammar.check(self._decls[k], v)
            if reason:
                details.append(grammar.detail(self._decls[k], reason))
                continue
            pending[k] = int(v) if p.type == "int" else v
        for p in self.table:
            if p.name in pending and p.applies_at_restart and not persist:
                details.append({"key": p.name, "reason": "requires_persist"})  # could never take effect

        if if_version is not None and if_version != self.config_version:
            conflict = self.config_json(device_id)
            conflict["error"] = "version_conflict"
            return ConfigReply(409, conflict)
        if details:
            return ConfigReply(400, {"error": "validation_failed", "applied": False, "details": details})
        if factory_reset:
            return ConfigReply(200, self.config_json(device_id), factory_reset=True)

        changed: list[str] = []
        restart = False
        for p in self.table:
            if p.name not in pending:
                continue
            if self.running[p.name] != pending[p.name] and not p.applies_at_restart:
                changed.append(p.name)
            self.running[p.name] = pending[p.name]
            if persist:
                self.stored[p.name] = pending[p.name]
                self.store.set(p.key, pending[p.name])
            if p.applies_at_restart and self.running[p.name] != self.booted[p.name]:
                restart = True
        if pending:
            self.config_version += 1                    # every accepted write, RAM-only included (§8.1)
            self.store.set(CONFIG_VERSION_KEY, self.config_version)
        return ConfigReply(200, self.config_json(device_id), restart=restart, changed=changed)
