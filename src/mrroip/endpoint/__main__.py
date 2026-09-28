# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
Runs an endpoint with a profile from this package.

    python3.12 -m mrroip.endpoint --profile reference --class stationary --http-port 8080 --state-dir /tmp/lamp
"""

import argparse
import logging
import os
import signal
import threading
import uuid
from collections.abc import Callable
from ipaddress import IPv4Address
from types import FrameType
from typing import Final

from .. import __version__
from .core import Endpoint
from .reference import Reference
from .server import Server
from .profile import Profile
from .store import FileStore, MemoryStore, Store

PROFILES: Final[dict[str, Callable[[str], Profile]]] = {"reference": Reference}


def default_device_id() -> str:
    node = uuid.getnode()           # a MAC of this host; with --device-id, several endpoints can share one host
    return ":".join(f"{(node >> s) & 0xff:02x}" for s in range(40, -8, -8))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default="reference", choices=sorted(PROFILES))
    ap.add_argument("--class", dest="device_class", default="passive", choices=["mobile", "stationary", "passive"])
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--state-dir", help="where stored parameters live; without it nothing survives a restart")
    ap.add_argument("--device-id", default=default_device_id(), help="aa:bb:cc:dd:ee:ff; default: this host's MAC")
    ap.add_argument("--iface", type=IPv4Address, help="the local IPv4 address facing the layout")
    ap.add_argument("--no-mdns", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")

    store: Store = FileStore(os.path.join(args.state_dir, "mrroip.json")) if args.state_dir else MemoryStore()

    def make(on_restart: Callable[[bool], None], on_name_changed: Callable[[], None]) -> Endpoint:
        return Endpoint(PROFILES[args.profile](args.device_class), args.device_id.lower(), store=store,
                        firmware=__version__, http_port=args.http_port, on_restart=on_restart,
                        on_name_changed=on_name_changed)

    server = Server(make, http_port=args.http_port, iface=args.iface, mdns=not args.no_mdns).start()
    done = threading.Event()
    def finish(signum: int, frame: FrameType | None) -> None:
        done.set()

    signal.signal(signal.SIGINT, finish)
    signal.signal(signal.SIGTERM, finish)
    done.wait()
    server.stop()
    store.flush()


if __name__ == "__main__":
    main()
