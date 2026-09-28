/*
 * MRRoIP endpoint core for ESP-IDF (MRROIP-CORE-SPEC.md): /definition, /config, /control over HTTP and UDP, /state,
 * authority and timeout, parameters in NVS, network bring-up and discovery. The application supplies the device
 * profile (the functions in mrroip_profile.h) and starts everything in this order:
 *
 *     mrroip_init();              // NVS, the flash writer, the parameter table (the profile's included)
 *     profile_start();            // the application's profile: parameters are loaded by now
 *     mrroip_start(&config);      // control, network (Ethernet first if configured, else Wi-Fi), HTTP, UDP, discovery
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

// Pins of an RMII PHY on the ESP32's own Ethernet MAC (CONFIG_MRROIP_ETHERNET)
typedef struct {
    int mdc_gpio;
    int mdio_gpio;
    int phy_reset_gpio;         // -1 if the PHY has no reset line
    int ref_clk_gpio;           // 50 MHz RMII clock input from the PHY
} mrroip_ethernet_pins_t;

typedef struct {
    const mrroip_ethernet_pins_t *ethernet;     // NULL: Wi-Fi only
} mrroip_config_t;

void mrroip_init(void);
void mrroip_start(const mrroip_config_t *config);

// Console commands the core handles, for an application's serial console. Returns false if `cmd` is not one of them.
#define MRROIP_CONSOLE_HELP "wifi [forget]"
bool mrroip_console_command(const char *cmd, const char *arg);

// Apply a /config body exactly as POST /config would (§8.2), e.g. from a console. The reply body, or "ok", goes into
// msg. Returns true if accepted; *restarting (may be NULL) tells whether the endpoint restarts to apply it.
bool mrroip_config_write(const char *json, char *msg, size_t size, bool *restarting);

// The master holding authority now, as an IPv4 address in network byte order, or 0 if none (§11). A profile
// that receives its own stream can check that packets come from that master. Safe to call from profile_apply():
// it takes no lock, so it cannot wait for the control lock the core already holds there.
uint32_t mrroip_master_ip(void);

// The Wi-Fi station MAC, lowercase and colon-separated (§4)
const char *mrroip_device_id(void);
// Restart after queued flash writes, with ssdp:byebye; erase the namespace first for a factory reset
void mrroip_restart(bool factory_reset);
