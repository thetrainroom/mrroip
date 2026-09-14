"""
Finding MMRoIP endpoints (MMROIP-CORE-SPEC.md §6): SSDP, which is normative, and the whois probe, which is
a diagnostic for networks that block multicast.

On a computer with several networks, multicast and broadcast leave through the default interface. Pass
`iface` (a local IPv4 address) to search from another one, or set MMROIP_IFACE for code you cannot change,
such as the conformance probe.
"""

import json
import os
import socket
import time

from . import protocol


def _iface(iface):
    return iface or os.environ.get("MMROIP_IFACE") or None


def ssdp_search(timeout=6.0, st=protocol.SSDP_ST, iface=None):
    """Returns {ip: {header: value}} for every MMRoIP endpoint that answers."""
    iface = _iface(iface)
    msg = ("M-SEARCH * HTTP/1.1\r\n"
           f"HOST: {protocol.SSDP_ADDR[0]}:{protocol.SSDP_ADDR[1]}\r\n"
           'MAN: "ssdp:discover"\r\n'
           "MX: 2\r\n"
           f"ST: {st}\r\n\r\n").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    if iface:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
        s.bind((iface, 0))                      # answers come back to this interface
    s.settimeout(1.0)
    found, t0 = {}, time.monotonic()
    try:
        for _ in range(3):
            s.sendto(msg, protocol.SSDP_ADDR)
            time.sleep(0.15)
        while time.monotonic() - t0 < timeout:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            hdr = {}
            for line in data.decode("utf8", "replace").split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    hdr[k.strip().upper()] = v.strip()
            if (protocol.TOKEN in hdr.get("USN", "").lower()
                    or protocol.TOKEN in hdr.get("ST", "").lower()
                    or "X-MMROIP-ID" in hdr):
                found[addr[0]] = hdr
    finally:
        s.close()
    return found


def whois(ip=None, timeout=3.0, iface=None):
    """Diagnostic probe. Unicast if ip is given, otherwise broadcast."""
    iface = _iface(iface)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if iface:
        s.bind((iface, 0))
    s.settimeout(0.6)
    target = (ip or "255.255.255.255", protocol.WHOIS_PORT)
    out, t0 = {}, time.monotonic()
    try:
        for _ in range(3):
            s.sendto(json.dumps({"m": "whois"}).encode(), target)
            time.sleep(0.1)
        while time.monotonic() - t0 < timeout:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            try:    out[addr[0]] = json.loads(data)
            except Exception: pass
    finally:
        s.close()
    return out
