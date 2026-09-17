"""
mmroip — Python client for MMRoIP endpoints (Model Railroad over IP).

    import mmroip

    hosts = mmroip.ssdp_search()                  # {ip: SSDP headers}
    mmroip.mdns_browse()                          # {ip: instance, host, port, TXT}
    dev = mmroip.Device("192.168.10.164")
    dev.definition()                              # what the endpoint is and accepts
    dev.control(mode="show")                      # desired state, over HTTP
    dev.control_udp(mode="hold")                  # the same over UDP

    w, h = dev.image_size()                       # display profile
    dev.show_image(mmroip.image.text("Gleis 3", w, h))          # 1-bit panels
    dev.put_image(mmroip.image.pattern_rgb565("bars", w, h))    # colour panels, PUT /objects/image
    mmroip.rtp.Sender(ip, 5004, w, h).send_frame(frame)         # moving pictures, RFC 4175 over RTP

Standard library only; Pillow is needed only by mmroip.image.text() and mmroip.image.picture().
"""

from . import image, protocol, rtp
from .device import Device
from .discovery import NotifyListener, mdns_browse, ssdp_search, whois

__all__ = ["Device", "NotifyListener", "mdns_browse", "ssdp_search", "whois", "image", "protocol", "rtp"]
__version__ = "0.1.0"
