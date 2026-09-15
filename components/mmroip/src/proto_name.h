/* proto_name.h — the ONLY place the protocol name is spelled (MMROIP-CORE-SPEC.md §0). */
#pragma once

#define MMROIP_NAME        "MMRoIP"          /* human-readable          */
#define MMROIP_TOKEN       "mmroip"          /* lowercase, identifiers  */
#define MMROIP_VERSION     "0.1"             /* draft revision spoken   */
#define MMROIP_UDP_PORT    5300
#define MMROIP_SSDP_ST     "urn:schemas-mmroip-org:device:Endpoint:1"
#define MMROIP_MDNS_SVC    "_mmroip"         /* + "._tcp"               */
#define MMROIP_HEADER      "X-MMROIP-"       /* SSDP header prefix      */
