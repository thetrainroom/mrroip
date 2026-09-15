/*
 * MMRoIP endpoint core for ESP-IDF (MMROIP-CORE-SPEC.md): /definition, /config, /control over HTTP and UDP, /state,
 * authority and timeout, parameters in NVS, network bring-up and discovery. The application supplies the device
 * profile (the functions in mmroip_profile.h) and starts everything in this order:
 *
 *     mmroip_init();              // NVS, the flash writer, the parameter table (the profile's included)
 *     profile_start();            // the application's profile: parameters are loaded by now
 *     mmroip_start(&config);      // control, network (Ethernet first if configured, else Wi-Fi), HTTP, UDP, discovery
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

// Pins of an RMII PHY on the ESP32's own Ethernet MAC (CONFIG_MMROIP_ETHERNET)
typedef struct {
    int mdc_gpio;
    int mdio_gpio;
    int phy_reset_gpio;         // -1 if the PHY has no reset line
    int ref_clk_gpio;           // 50 MHz RMII clock input from the PHY
} mmroip_ethernet_pins_t;

typedef struct {
    const mmroip_ethernet_pins_t *ethernet;     // NULL: Wi-Fi only
} mmroip_config_t;

void mmroip_init(void);
void mmroip_start(const mmroip_config_t *config);

// Console commands the core handles, for an application's serial console. Returns false if `cmd` is not one of them.
#define MMROIP_CONSOLE_HELP "wifi [forget]"
bool mmroip_console_command(const char *cmd, const char *arg);

// Apply a /config body exactly as POST /config would (§8.2), e.g. from a console. The reply body, or "ok", goes into
// msg. Returns true if accepted; *restarting (may be NULL) tells whether the endpoint restarts to apply it.
bool mmroip_config_write(const char *json, char *msg, size_t size, bool *restarting);

// The Wi-Fi station MAC, lowercase and colon-separated (§4)
const char *mmroip_device_id(void);
// Restart after queued flash writes, with ssdp:byebye; erase the namespace first for a factory reset
void mmroip_restart(bool factory_reset);
