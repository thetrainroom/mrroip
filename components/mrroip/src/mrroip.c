/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdio.h>
#include <stdlib.h>
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

// A device updated from firmware older than the rename keeps what it stored. The keys are listed first and copied
// after, since writing while an iterator is open can move NVS pages under it. cfg_ver goes last: a device counts
// as migrated only once it is there, so a copy that fails part-way is tried again at the next start, and the legacy
// namespace is erased only after everything arrived. Runs before any task starts, so writing here does not bypass
// the store's queue (§8.3).
#define LEGACY_KEYS_MAX 48
#define CONFIG_VERSION_KEY "cfg_ver"

typedef struct {
    char key[NVS_KEY_NAME_MAX_SIZE];
    nvs_type_t type;
} legacy_entry_t;

static bool copy_entry(nvs_handle_t from, nvs_handle_t to, const legacy_entry_t *e)
{
    if (e->type == NVS_TYPE_I32) {
        int32_t v;
        return nvs_get_i32(from, e->key, &v) == ESP_OK && nvs_set_i32(to, e->key, v) == ESP_OK;
    }
    if (e->type == NVS_TYPE_U32) {
        uint32_t v;
        return nvs_get_u32(from, e->key, &v) == ESP_OK && nvs_set_u32(to, e->key, v) == ESP_OK;
    }
    if (e->type == NVS_TYPE_STR) {
        size_t len = 0;
        if (nvs_get_str(from, e->key, NULL, &len) != ESP_OK) {
            return false;
        }
        char *v = malloc(len);
        bool ok = v && nvs_get_str(from, e->key, v, &len) == ESP_OK && nvs_set_str(to, e->key, v) == ESP_OK;
        free(v);
        return ok;
    }
    return false;                       // the core never stored other types
}

static void migrate_legacy_namespace(void)
{
    nvs_handle_t current;
    if (nvs_open(MRROIP_TOKEN, NVS_READWRITE, &current) != ESP_OK) {
        return;
    }
    uint32_t version;
    nvs_handle_t legacy;
    // read-only first: opening read-write would create the namespace on a device that never had one
    if (nvs_get_u32(current, CONFIG_VERSION_KEY, &version) == ESP_OK ||
        nvs_open(LEGACY_NAMESPACE, NVS_READONLY, &legacy) != ESP_OK) {
        nvs_close(current);             // migrated, or nothing to migrate
        return;
    }

    static legacy_entry_t entries[LEGACY_KEYS_MAX];
    size_t count = 0;
    bool listed = true;
    nvs_iterator_t it = NULL;
    for (esp_err_t err = nvs_entry_find(NVS_DEFAULT_PART_NAME, LEGACY_NAMESPACE, NVS_TYPE_ANY, &it);
         err == ESP_OK; err = nvs_entry_next(&it)) {
        nvs_entry_info_t info;
        nvs_entry_info(it, &info);
        if (count == LEGACY_KEYS_MAX) {
            listed = false;
            break;
        }
        strlcpy(entries[count].key, info.key, sizeof(entries[count].key));
        entries[count].type = info.type;
        count++;
    }
    nvs_release_iterator(it);

    int copied = 0, failed = listed ? 0 : 1;
    const legacy_entry_t *version_entry = NULL;
    for (size_t i = 0; i < count; i++) {
        if (strcmp(entries[i].key, CONFIG_VERSION_KEY) == 0) {
            version_entry = &entries[i];
            continue;
        }
        copy_entry(legacy, current, &entries[i]) ? copied++ : failed++;
    }
    if (failed == 0 && nvs_commit(current) == ESP_OK) {
        // last: from here on the device counts as migrated
        uint32_t zero_version = 0;
        bool marked = version_entry ? copy_entry(legacy, current, version_entry)
                                    : nvs_set_u32(current, CONFIG_VERSION_KEY, zero_version) == ESP_OK;
        nvs_handle_t erase;
        if (marked && nvs_commit(current) == ESP_OK) {
            copied += version_entry ? 1 : 0;
            if (nvs_open(LEGACY_NAMESPACE, NVS_READWRITE, &erase) == ESP_OK) {
                nvs_erase_all(erase);
                nvs_commit(erase);
                nvs_close(erase);
            }
        } else {
            failed++;
        }
    }
    if (copied || failed) {
        ESP_LOGW(TAG, "%d settings moved from namespace '%s' to '%s'%s", copied, LEGACY_NAMESPACE, MRROIP_TOKEN,
                 failed ? "; some failed, trying again at the next start" : "");
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
