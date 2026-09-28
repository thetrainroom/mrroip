/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdio.h>
#include <string.h>
#include "cJSON.h"
#include "esp_err.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "control.h"
#include "discovery.h"
#include "http_api.h"
#include "mrroip.h"
#include "mrroip_params.h"
#include "mrroip_profile.h"
#include "net.h"
#include "proto_name.h"
#include "store.h"
#include "udp_control.h"

static const char *TAG = "mrroip";

// Before 2026-09-28 the name was misspelt, and the token with it: devices flashed before then keep their
// Wi-Fi credentials and settings under this namespace. Read once, then erased.
#define LEGACY_NAMESPACE "mmroip"

// A device updated from firmware older than the rename keeps what it stored: when the current namespace holds no
// configuration yet, every entry of the legacy one is copied over, and the legacy one erased once the copy is
// committed. Runs before any task starts, so writing here does not bypass the store's queue (§8.3).
static void migrate_legacy_namespace(void)
{
    nvs_handle_t current;
    if (nvs_open(MRROIP_TOKEN, NVS_READWRITE, &current) != ESP_OK) {
        return;
    }
    uint32_t version;
    char ssid[33];
    size_t ssid_len = sizeof(ssid);
    bool configured = nvs_get_u32(current, "cfg_ver", &version) == ESP_OK ||
                      nvs_get_str(current, "wifi_ssid", ssid, &ssid_len) == ESP_OK;
    nvs_handle_t legacy;
    if (configured || nvs_open(LEGACY_NAMESPACE, NVS_READWRITE, &legacy) != ESP_OK) {
        nvs_close(current);
        return;
    }
    int copied = 0, failed = 0;
    nvs_iterator_t it = NULL;
    esp_err_t err = nvs_entry_find(NVS_DEFAULT_PART_NAME, LEGACY_NAMESPACE, NVS_TYPE_ANY, &it);
    while (err == ESP_OK) {
        nvs_entry_info_t info;
        nvs_entry_info(it, &info);
        bool ok = true;
        if (info.type == NVS_TYPE_I32) {
            int32_t v;
            ok = nvs_get_i32(legacy, info.key, &v) == ESP_OK && nvs_set_i32(current, info.key, v) == ESP_OK;
        } else if (info.type == NVS_TYPE_U32) {
            uint32_t v;
            ok = nvs_get_u32(legacy, info.key, &v) == ESP_OK && nvs_set_u32(current, info.key, v) == ESP_OK;
        } else if (info.type == NVS_TYPE_STR) {
            char v[128];
            size_t len = sizeof(v);
            ok = nvs_get_str(legacy, info.key, v, &len) == ESP_OK && nvs_set_str(current, info.key, v) == ESP_OK;
        } else {
            ok = false;                 // the core never stored other types
        }
        ok ? copied++ : failed++;
        err = nvs_entry_next(&it);
    }
    nvs_release_iterator(it);
    if (failed == 0 && nvs_commit(current) == ESP_OK) {
        nvs_erase_all(legacy);
        nvs_commit(legacy);
    }
    if (copied || failed) {
        ESP_LOGW(TAG, "%d settings moved from namespace '%s' to '%s'%s", copied, LEGACY_NAMESPACE, MRROIP_TOKEN,
                 failed ? ", some failed: the old namespace is kept" : "");
    }
    nvs_close(legacy);
    nvs_close(current);
}

void mrroip_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    migrate_legacy_namespace();
    store_start();
    params_init();
}

void mrroip_start(const mrroip_config_t *config)
{
    control_start();
    net_init(profile_info()->device_type, config ? config->ethernet : NULL);
    http_api_start();       // before the network: the setup portal registers on this server
    net_start();
    udp_control_start();
    discovery_start();
}

uint32_t mrroip_master_ip(void)
{
    return control_master_ip();
}

const char *mrroip_device_id(void)
{
    return http_api_device_id();
}

void mrroip_restart(bool factory_reset)
{
    http_api_restart_after_reply(factory_reset);
}

bool mrroip_config_write(const char *json, char *msg, size_t size, bool *restarting)
{
    config_reply_t reply;
    params_config_apply(json, strlen(json), http_api_device_id(), &reply);
    bool ok = (reply.result == CONFIG_OK);
    bool restart = ok && (reply.restart || reply.factory_reset);
    char *text = reply.body ? cJSON_PrintUnformatted(reply.body) : NULL;
    snprintf(msg, size, "%s", text ? text : ok ? "ok" : "rejected");
    cJSON_free(text);
    cJSON_Delete(reply.body);
    if (restarting) {
        *restarting = restart;
    }
    if (restart) {
        http_api_restart_after_reply(reply.factory_reset);
    }
    return ok;
}

static void print_network(void)
{
    net_status_t net;
    net_get_status(&net);
#if CONFIG_MRROIP_ETHERNET
    if (net.wired) {
        printf("ethernet: connected, ip %s; Wi-Fi off while the cable is in\n", net.ip);
        return;
    }
    printf("ethernet: %s\n", net.eth_link ? "link up, waiting for an address" : "no cable");
#endif
    switch (net.state) {
    case NET_STARTING:
        printf("wifi: starting\n");
        break;
    case NET_CONNECTING:
        printf("wifi: connecting to '%s' (%s%d/%d)\n", net.ssid, net.attempt ? "try " : "reconnecting, try ",
               net.attempt, NET_STA_ATTEMPTS);
        break;
    case NET_CONNECTED:
        printf("wifi: connected to '%s', ip %s\n", net.ssid, net.ip);
        break;
    case NET_SETUP_AP:
        printf("wifi: setup AP '%s', password '%s', form at http://192.168.4.1/, %d client(s)%s%s%s\n",
               net.ap_ssid, net.ap_pass, net.ap_clients,
               net.ssid[0] ? ", retrying '" : "", net.ssid, net.ssid[0] ? "' every 5 min" : "");
        break;
    }
}

bool mrroip_console_command(const char *cmd, const char *arg)
{
    if (strcmp(cmd, "wifi") != 0) {
        return false;
    }
    if (arg && strcmp(arg, "forget") == 0) {
        esp_err_t err = net_forget_credentials();
        if (err != ESP_OK) {
            printf("error: erasing credentials failed: %s\n", esp_err_to_name(err));
        } else {
            printf("wifi: credentials erased, restarting into setup mode\n");
            http_api_restart_after_reply(false);
        }
    } else if (arg) {
        printf("usage: wifi | wifi forget\n");
    } else {
        print_network();
    }
    return true;
}
