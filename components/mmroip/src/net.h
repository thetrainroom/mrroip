/*
 * Network bring-up, internal to the core. The state and the credential functions are public in mmroip_net.h.
 */
#pragma once

#include "mmroip.h"
#include "mmroip_net.h"

// Initialise the network stack and Wi-Fi driver. The setup access point is named "<device_type>-XXXXXX".
// ethernet: the PHY pins for CONFIG_MMROIP_ETHERNET, or NULL for Wi-Fi only.
void net_init(const char *device_type, const mmroip_ethernet_pins_t *ethernet);
// Bring the network up in the background; the HTTP server must already be running
void net_start(void);
