/*
 * Wi-Fi setup portal for setup-AP mode: a form for the credentials on the shared HTTP server (http_api.c),
 * and a DNS server answering every name with the AP address, so a phone that joins opens the form by itself.
 */
#pragma once

#include <stddef.h>
#include "esp_http_server.h"

// ssids: networks to offer in the form; the array and ap_ssid must stay valid while the portal runs
void portal_start(httpd_handle_t server, const char ssids[][33], size_t count, const char *ap_ssid);

// Answer a request for an unknown URL in setup mode by sending the phone to the form
esp_err_t portal_redirect(httpd_req_t *req);
