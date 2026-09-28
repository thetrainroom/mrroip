/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * Network bring-up, internal to the core. The state and the credential functions are public in mrroip_net.h.
 */
#pragma once

#include "mrroip.h"
#include "mrroip_net.h"

// Initialise the network stack and Wi-Fi driver. The setup access point is named "<device_type>-XXXXXX".
// ethernet: the PHY pins for CONFIG_MRROIP_ETHERNET, or NULL for Wi-Fi only.
void net_init(const char *device_type, const mrroip_ethernet_pins_t *ethernet);
// Bring the network up in the background; the HTTP server must already be running
void net_start(void);
