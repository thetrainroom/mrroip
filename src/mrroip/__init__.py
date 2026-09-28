# SPDX-FileCopyrightText: 2026 Thierry Gschwind
# SPDX-License-Identifier: Apache-2.0
"""
mrroip — Python client for MRRoIP endpoints (Model Railroad over IP).

    import mrroip

    hosts = mrroip.ssdp_search()                  # {ip: SSDP headers}
    mrroip.mdns_browse()                          # {ip: instance, host, port, TXT}
    dev = mrroip.Device("192.168.10.164")
    dev.definition()                              # what the endpoint is and accepts
    dev.control(mode="show")                      # desired state, over HTTP
    dev.control_udp(mode="hold")                  # the same over UDP

    w, h = dev.image_size()                       # display profile
    dev.show_image(mrroip.image.text("Gleis 3", w, h))          # 1-bit panels
    dev.put_image(mrroip.image.pattern_rgb565("bars", w, h))    # colour panels, PUT /objects/image
    mrroip.rtp.Sender(ip, 5004, w, h).send_frame(frame)         # moving pictures, RFC 4175 over RTP

Standard library only; Pillow is needed only by mrroip.image.text() and mrroip.image.picture().
"""

from . import image, protocol, rtp
from .device import Device
from .discovery import MdnsService, NotifyEvent, NotifyListener, WhoisReply, mdns_browse, ssdp_search, whois

__all__ = ["Device", "MdnsService", "NotifyEvent", "NotifyListener", "WhoisReply", "mdns_browse", "ssdp_search",
           "whois", "image", "protocol", "rtp"]
__version__ = "0.2.0"
