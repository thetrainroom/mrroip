/*
 * Network state for profiles and applications; the bring-up itself is internal (src/net.c). With Ethernet (CONFIG_MMROIP_ETHERNET) a cable with a link and a DHCP address always wins:
 * Wi-Fi is switched off while it lasts, and comes back when the cable goes. Wi-Fi per MMROIP-CORE-SPEC.md
 * §5.1: stored credentials -> station mode (3 attempts of 20 s), otherwise a WPA2 setup access point with a
 * captive portal (portal.c). Credentials live in NVS namespace "mmroip", keys wifi_ssid / wifi_pass (§12),
 * never in the Wi-Fi driver's own storage.
 */
#pragma once

#include <stdbool.h>
#include "esp_err.h"

#define NET_STA_ATTEMPTS    3

typedef enum {
    NET_STARTING,
    NET_CONNECTING,     // trying the stored network; attempt 0 means reconnecting after a loss
    NET_CONNECTED,      // Ethernet or station has an IP address
    NET_SETUP_AP,       // setup access point and portal are up
} net_state_t;

typedef struct {
    net_state_t state;
    bool wired;         // NET_CONNECTED over Ethernet; Wi-Fi is off
    bool eth_link;      // a cable is plugged in and linked (always false without Ethernet)
    int attempt;
    char ssid[33];      // stored network
    char ip[16];        // the address in use while connected
    char ap_ssid[48];
    char ap_pass[16];
    int ap_clients;
} net_status_t;

void net_get_status(net_status_t *out);

// SSID 1..32 bytes; password empty for an open network, otherwise 8..63 characters (WPA2)
esp_err_t net_validate_credentials(const char *ssid, const char *pass);
esp_err_t net_store_credentials(const char *ssid, const char *pass);
esp_err_t net_forget_credentials(void);
