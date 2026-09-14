"""
mmroip — Python client for MMRoIP endpoints (Model Railroad over IP).

    import mmroip

    hosts = mmroip.ssdp_search()                  # {ip: SSDP headers}
    dev = mmroip.Device("192.168.10.164")
    dev.definition()                              # what the endpoint is and accepts
    dev.control(mode="show")                      # desired state, over HTTP
    dev.control_udp(mode="hold")                  # the same over UDP

    w, h = dev.image_size()                       # display profile
    dev.show_image(mmroip.image.text("Gleis 3", w, h))

Standard library only; Pillow is needed only by mmroip.image.text() and mmroip.image.picture().
"""

from . import image, protocol
from .device import Device
from .discovery import ssdp_search, whois

__all__ = ["Device", "ssdp_search", "whois", "image", "protocol"]
__version__ = "0.1.0"
