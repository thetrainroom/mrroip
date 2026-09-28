/*
 * HTTP transport for the MRRoIP endpoints (MRROIP-1.md §7–§10): /definition, /config and /state.
 * The same server runs in station and setup-AP mode (§5.2); the setup portal registers its form on it.
 */
#pragma once

#include <stdbool.h>
#include "esp_http_server.h"

void http_api_start(void);
httpd_handle_t http_api_server(void);
// Wi-Fi station MAC, lowercase, colon-separated (§5.1)
const char *http_api_device_id(void);
// Restart shortly, after queued flash writes are done; erase the namespace first for a factory reset
void http_api_restart_after_reply(bool factory_reset);
