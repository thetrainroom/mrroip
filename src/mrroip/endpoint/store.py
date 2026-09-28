# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
Where parameters are stored (MRROIP-1.md §8.3): one JSON file per endpoint, the same keys NVS would hold.
Writes are queued to one writer thread, never done from a request handler, and the file is replaced
atomically so a power cut leaves either the old values or the new ones.
"""

import json
import os
import queue
import tempfile
import threading
from dataclasses import dataclass
from typing import Protocol

from .._types import Json, JsonObject, as_object


class Store(Protocol):
    """Where stored parameters live. set() and erase_all() may return before the write is done; flush() waits."""

    def load(self) -> JsonObject: ...

    def set(self, key: str, value: Json) -> None: ...

    def erase_all(self) -> None: ...

    def flush(self, timeout: float = 5.0) -> bool: ...


class MemoryStore:
    """Nothing survives the process: for tests, and for an endpoint started without a state directory."""

    def __init__(self, values: JsonObject | None = None) -> None:
        self.values: JsonObject = dict(values or {})

    def load(self) -> JsonObject:
        return dict(self.values)

    def set(self, key: str, value: Json) -> None:
        self.values[key] = value

    def erase_all(self) -> None:
        self.values.clear()

    def flush(self, timeout: float = 5.0) -> bool:
        return True


@dataclass
class _Set:
    key: str
    value: Json


class _Erase:
    pass


@dataclass
class _Flush:
    done: threading.Event


_Op = _Set | _Erase | _Flush


class FileStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._values = self.load()
        self._queue: queue.Queue[_Op] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="store", daemon=True)
        self._thread.start()

    def load(self) -> JsonObject:
        try:
            with open(self.path, encoding="utf8") as f:
                values = json.load(f)
            return as_object(values) or {}
        except (OSError, ValueError):
            return {}

    def set(self, key: str, value: Json) -> None:
        self._queue.put(_Set(key, value))

    def erase_all(self) -> None:
        self._queue.put(_Erase())

    def flush(self, timeout: float = 5.0) -> bool:
        done = threading.Event()
        self._queue.put(_Flush(done))
        return done.wait(timeout)

    def _run(self) -> None:
        while True:
            match self._queue.get():
                case _Set(key, value):
                    if key in self._values and self._values[key] == value:
                        continue                # only changed values are written (§8.2)
                    self._values[key] = value
                case _Erase():
                    self._values = {}
                case _Flush(done):
                    done.set()                  # everything queued before it is written
                    continue
            self._write()

    def _write(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mrroip-")
        with os.fdopen(fd, "w", encoding="utf8") as f:
            json.dump(self._values, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
