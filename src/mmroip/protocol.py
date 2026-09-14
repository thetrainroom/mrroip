"""
Protocol constants (MMROIP-CORE-SPEC.md §0, §5.2, §9.3). The name is spelled here and nowhere else in
the package, so a rename is a one-line diff.
"""

NAME = "MMRoIP"
TOKEN = "mmroip"
VERSION = "0.1"

UDP_PORT = 5300
WHOIS_PORT = 8266
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_ST = "urn:schemas-mmroip-org:device:Endpoint:1"
HEADER_PREFIX = "X-MMROIP-"        # X-MMROIP-ID, -NAME, -TYPE, -CLASS (§6.1)
MDNS_SERVICE = "_mmroip"           # + "._tcp" (§6.2)

CORE_MODES = ["estop", "reset", "release", "hold"]
