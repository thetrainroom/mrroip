# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
An MRRoIP endpoint core in Python (MRROIP-1.md §5–§13), for hosts with more memory than a microcontroller:
a Raspberry Pi, a PC, a test bench. It behaves like the ESP-IDF core in components/mrroip/ and is checked
against the same conformance vectors and probe. Standard library only.

    python3.12 -m mrroip.endpoint --profile reference --class stationary --http-port 8080

A device is a Profile subclass (profile.py); the core never knows what it controls.
"""

from .core import Endpoint, Response
from .params import Param
from .profile import Profile
from .server import Server
from .store import FileStore, MemoryStore

__all__ = ["Endpoint", "FileStore", "MemoryStore", "Param", "Profile", "Response", "Server"]
