#include <stdio.h>
#include <string.h>
#include "cJSON.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "control.h"
#include "discovery.h"
#include "http_api.h"
#include "mmroip.h"
#include "mmroip_params.h"
#include "mmroip_profile.h"
#include "net.h"
#include "store.h"
#include "udp_control.h"

void mmroip_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    store_start();
    params_init();
}

void mmroip_start(const mmroip_config_t *config)
{
    control_start();
    net_init(profile_info()->device_type, config ? config->ethernet : NULL);
    http_api_start();       // before the network: the setup portal registers on this server
    net_start();
    udp_control_start();
    discovery_start();
}

const char *mmroip_device_id(void)
{
    return http_api_device_id();
}

void mmroip_restart(bool factory_reset)
{
    http_api_restart_after_reply(factory_reset);
}

bool mmroip_config_write(const char *json, char *msg, size_t size, bool *restarting)
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
#if CONFIG_MMROIP_ETHERNET
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

bool mmroip_console_command(const char *cmd, const char *arg)
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
