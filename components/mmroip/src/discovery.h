/*
 * Discovery (MMROIP-CORE-SPEC.md §6): SSDP announcements and M-SEARCH answers (normative), the whois probe on
 * UDP 8266 (diagnostic), and mDNS (convenience; compiled out with CONFIG_MMROIP_MDNS=n).
 */
#pragma once

void discovery_start(void);

// device_name changed: re-announce over SSDP and re-register mDNS (§8.2)
void discovery_name_changed(void);

// A /control or /config request arrived; restarts the 30-minute announcement recovery window (§6.1)
void discovery_note_traffic(void);

// Send ssdp:byebye before a clean restart
void discovery_byebye(void);
