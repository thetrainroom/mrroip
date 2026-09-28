/* proto_name.h — the ONLY place the protocol name is spelled (MRROIP-1.md §2.4). */
#pragma once

#define MRROIP_NAME        "MRRoIP"          /* human-readable          */
#define MRROIP_TOKEN       "mrroip"          /* lowercase, identifiers  */
#define MRROIP_VERSION     "0.1"             /* draft revision spoken   */
#define MRROIP_UDP_PORT    5300
#define MRROIP_SSDP_ST     "urn:schemas-mrroip-org:device:Endpoint:1"
#define MRROIP_MDNS_SVC    "_mrroip"         /* + "._tcp"               */
#define MRROIP_HEADER      "X-MRROIP-"       /* SSDP header prefix      */
