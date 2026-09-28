/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdbool.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs.h"
#include "sdkconfig.h"
#if CONFIG_MRROIP_ETHERNET
#include "esp_eth.h"
#endif
#include "http_api.h"
#include "net.h"
#include "portal.h"
#include "proto_name.h"

static const char *TAG = "net";

#define NVS_NAMESPACE           MRROIP_TOKEN
#define STA_ATTEMPT_TIMEOUT_MS  20000
#define AP_RETRY_INTERVAL_MS    (5 * 60 * 1000)
#define AP_MAX_CLIENTS          4
#define AP_CHANNEL              1
#define AP_PASS_LEN             8
#define SCAN_MAX                16
#define ETH_LINK_WAIT_MS        4000    // at boot: the IP101 finishes autonegotiation in about 2 s
#define ETH_DHCP_WAIT_MS        15000   // at boot, with a link: how long Wi-Fi waits for the cable's address
#define ETH_CHECK_MS            2000

#define GOT_IP_BIT              BIT0
#define DISCONNECTED_BIT        BIT1
#define ETH_CHANGED_BIT         BIT2    // cable link or address changed: look at eth_ready() again

static SemaphoreHandle_t lock;
static EventGroupHandle_t events;
static net_status_t status;
static bool keep_connected;             // set after the first successful join: reconnect on every loss
static char device_type_name[16];
static char scan_ssids[SCAN_MAX][33];   // networks seen before the AP came up, for the portal's list
static size_t scan_count;
static bool wifi_started;
#if CONFIG_MRROIP_ETHERNET
static esp_netif_t *eth_netif;
static mrroip_ethernet_pins_t eth_pins;
static bool eth_configured;
#endif

void net_get_status(net_status_t *out)
{
    if (!lock) {
        memset(out, 0, sizeof(*out));
        return;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    *out = status;
    xSemaphoreGive(lock);
}

static void set_state(net_state_t state, int attempt)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    status.state = state;
    status.attempt = attempt;
    xSemaphoreGive(lock);
}

static bool nvs_read_str(const char *key, char *out, size_t size)
{
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) != ESP_OK) {
        return false;
    }
    size_t len = size;
    esp_err_t err = nvs_get_str(nvs, key, out, &len);
    nvs_close(nvs);
    return err == ESP_OK;
}

esp_err_t net_validate_credentials(const char *ssid, const char *pass)
{
    size_t ssid_len = strlen(ssid);
    size_t pass_len = strlen(pass);
    if (ssid_len == 0 || ssid_len > 32 || (pass_len != 0 && (pass_len < 8 || pass_len > 63))) {
        return ESP_ERR_INVALID_ARG;
    }
    return ESP_OK;
}

esp_err_t net_store_credentials(const char *ssid, const char *pass)
{
    esp_err_t err = net_validate_credentials(ssid, pass);
    if (err != ESP_OK) {
        return err;
    }
    nvs_handle_t nvs;
    err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_str(nvs, "wifi_ssid", ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(nvs, "wifi_pass", pass);
    }
    if (err == ESP_OK) {
        err = nvs_commit(nvs);
    }
    nvs_close(nvs);
    return err;
}

esp_err_t net_forget_credentials(void)
{
    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs);
    if (err != ESP_OK) {
        return err;
    }
    nvs_erase_key(nvs, "wifi_ssid");    // ESP_ERR_NVS_NOT_FOUND is fine: nothing to forget
    nvs_erase_key(nvs, "wifi_pass");
    err = nvs_commit(nvs);
    nvs_close(nvs);
    return err;
}

// One random password per device, generated once and shown on the display. Not derived from the MAC,
// half of which is visible in the SSID. The alphabet leaves out 0/o and 1/i/l, which a 5x7 font confuses.
static void load_ap_password(char *out, size_t size)
{
    if (nvs_read_str("ap_pass", out, size) && strlen(out) >= 8) {
        return;
    }
    static const char alphabet[] = "abcdefghjkmnpqrstuvwxyz23456789";
    for (int i = 0; i < AP_PASS_LEN; i++) {
        out[i] = alphabet[esp_random() % (sizeof(alphabet) - 1)];  // RF is on by now, so esp_random() has real entropy
    }
    out[AP_PASS_LEN] = '\0';

    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) == ESP_OK) {
        if (nvs_set_str(nvs, "ap_pass", out) != ESP_OK || nvs_commit(nvs) != ESP_OK) {
            ESP_LOGE(TAG, "Saving the AP password failed; a new one is generated at the next boot");
        }
        nvs_close(nvs);
    }
}

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupSetBits(events, DISCONNECTED_BIT);
        xSemaphoreTake(lock, portMAX_DELAY);
        bool reconnect = keep_connected && !status.wired;
        if (reconnect) {
            status.state = NET_CONNECTING;
            status.attempt = 0;
            status.ip[0] = '\0';
        }
        xSemaphoreGive(lock);
        if (reconnect) {
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *got = data;
        xSemaphoreTake(lock, portMAX_DELAY);
        if (!status.wired) {
            esp_ip4addr_ntoa(&got->ip_info.ip, status.ip, sizeof(status.ip));
            if (status.state != NET_SETUP_AP) {     // in setup mode net_task restarts instead
                status.state = NET_CONNECTED;
                status.attempt = 0;
            }
        }
        xSemaphoreGive(lock);
        xEventGroupSetBits(events, GOT_IP_BIT);
#if CONFIG_MRROIP_ETHERNET
    } else if (base == ETH_EVENT && (id == ETHERNET_EVENT_CONNECTED || id == ETHERNET_EVENT_DISCONNECTED)) {
        xSemaphoreTake(lock, portMAX_DELAY);
        status.eth_link = (id == ETHERNET_EVENT_CONNECTED);
        xSemaphoreGive(lock);
        xEventGroupSetBits(events, ETH_CHANGED_BIT);
    } else if (base == IP_EVENT && (id == IP_EVENT_ETH_GOT_IP || id == IP_EVENT_ETH_LOST_IP)) {
        xEventGroupSetBits(events, ETH_CHANGED_BIT);
#endif
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STACONNECTED) {
        xSemaphoreTake(lock, portMAX_DELAY);
        status.ap_clients++;
        xSemaphoreGive(lock);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STADISCONNECTED) {
        xSemaphoreTake(lock, portMAX_DELAY);
        if (status.ap_clients > 0) {
            status.ap_clients--;
        }
        xSemaphoreGive(lock);
    }
}

// The cable has a link and DHCP has given it an address
static bool eth_ready(char *ip_out)
{
#if CONFIG_MRROIP_ETHERNET
    xSemaphoreTake(lock, portMAX_DELAY);
    bool link = status.eth_link;
    xSemaphoreGive(lock);
    esp_netif_ip_info_t info;
    if (link && eth_netif && esp_netif_get_ip_info(eth_netif, &info) == ESP_OK && info.ip.addr != 0) {
        if (ip_out) {
            esp_ip4addr_ntoa(&info.ip, ip_out, 16);
        }
        return true;
    }
#endif
    return false;
}

// Waits until eth_ready() or `ms` have passed. Without Ethernet it just sleeps.
static bool wait_eth_ready(int ms)
{
    int64_t end_ms = esp_timer_get_time() / 1000 + ms;
    while (1) {
        xEventGroupClearBits(events, ETH_CHANGED_BIT);      // before looking, so no change is missed
        if (eth_ready(NULL)) {
            return true;
        }
        int64_t left_ms = end_ms - esp_timer_get_time() / 1000;
        if (left_ms <= 0) {
            return false;
        }
        xEventGroupWaitBits(events, ETH_CHANGED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(left_ms));
    }
}

typedef enum { JOIN_OK, JOIN_FAILED, JOIN_CABLE } join_t;

static join_t try_station(int timeout_ms)
{
    xEventGroupClearBits(events, GOT_IP_BIT | DISCONNECTED_BIT | ETH_CHANGED_BIT);
    if (eth_ready(NULL)) {
        return JOIN_CABLE;
    }
    esp_wifi_connect();
    EventBits_t bits = xEventGroupWaitBits(events, GOT_IP_BIT | DISCONNECTED_BIT | ETH_CHANGED_BIT, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(timeout_ms));
    if (bits & GOT_IP_BIT) {
        return JOIN_OK;
    }
    esp_wifi_disconnect();
    vTaskDelay(pdMS_TO_TICKS(1000));        // let the disconnect event land before the next attempt
    if ((bits & ETH_CHANGED_BIT) && eth_ready(NULL)) {
        return JOIN_CABLE;
    }
    return (bits & ETH_CHANGED_BIT) ? try_station(timeout_ms) : JOIN_FAILED;   // a link without an address yet
}

static void scan_networks(void)
{
    static wifi_ap_record_t records[SCAN_MAX];
    uint16_t found = SCAN_MAX;
    scan_count = 0;
    if (esp_wifi_scan_start(NULL, true) != ESP_OK || esp_wifi_scan_get_ap_records(&found, records) != ESP_OK) {
        ESP_LOGW(TAG, "Wi-Fi scan failed; the portal offers a name field only");
        return;
    }
    for (uint16_t i = 0; i < found && scan_count < SCAN_MAX; i++) {
        const char *ssid = (const char *)records[i].ssid;
        bool seen = (ssid[0] == '\0');          // hidden networks have no name to offer
        for (size_t j = 0; j < scan_count && !seen; j++) {
            seen = (strcmp(scan_ssids[j], ssid) == 0);
        }
        if (!seen) {
            memcpy(scan_ssids[scan_count++], records[i].ssid, sizeof(scan_ssids[0]));
        }
    }
}

static void start_setup_ap(void)
{
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_STA, mac);     // named after the station MAC, which is the device identity (§4)

    xSemaphoreTake(lock, portMAX_DELAY);
    snprintf(status.ap_ssid, sizeof(status.ap_ssid), "%s-%02x%02x%02x", device_type_name, mac[3], mac[4], mac[5]);
    load_ap_password(status.ap_pass, sizeof(status.ap_pass));
    xSemaphoreGive(lock);

    scan_networks();                        // before any phone joins: scanning hops channels and would drop it

    esp_netif_create_default_wifi_ap();     // 192.168.4.1 with a DHCP server
    wifi_config_t ap = {
        .ap = {
            .channel = AP_CHANNEL,
            .authmode = WIFI_AUTH_WPA2_PSK,
            .max_connection = AP_MAX_CLIENTS,
        },
    };
    size_t ssid_len = strlen(status.ap_ssid);
    memcpy(ap.ap.ssid, status.ap_ssid, ssid_len);
    ap.ap.ssid_len = ssid_len;
    memcpy(ap.ap.password, status.ap_pass, strlen(status.ap_pass));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));

    portal_start(http_api_server(), (const char (*)[33])scan_ssids, scan_count, status.ap_ssid);
    set_state(NET_SETUP_AP, 0);
    ESP_LOGW(TAG, "Setup AP '%s' up, %u networks listed", status.ap_ssid, (unsigned)scan_count);
}

// Called with the cable ready: use it, and switch Wi-Fi off so the device has one address on the layout network
static void use_ethernet(void)
{
    char ip[16] = "";
    eth_ready(ip);
    xSemaphoreTake(lock, portMAX_DELAY);
    keep_connected = false;                 // before stopping, so the disconnect event does not reconnect
    status.wired = true;
    status.state = NET_CONNECTED;
    status.attempt = 0;
    strlcpy(status.ip, ip, sizeof(status.ip));
    xSemaphoreGive(lock);
    if (wifi_started) {
        esp_wifi_stop();
        wifi_started = false;
    }
    ESP_LOGW(TAG, "Ethernet up, IP %s; Wi-Fi off", ip);

    // Stay while the cable lasts; follow a new DHCP address
    while (wait_eth_ready(ETH_CHECK_MS)) {
        if (eth_ready(ip)) {
            xSemaphoreTake(lock, portMAX_DELAY);
            strlcpy(status.ip, ip, sizeof(status.ip));
            xSemaphoreGive(lock);
        }
        xEventGroupWaitBits(events, ETH_CHANGED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(ETH_CHECK_MS));
    }

    xSemaphoreTake(lock, portMAX_DELAY);
    status.wired = false;
    status.state = NET_STARTING;
    status.ip[0] = '\0';
    xSemaphoreGive(lock);
    ESP_LOGW(TAG, "Ethernet gone; back to Wi-Fi");
}

static void start_wifi(void)
{
    if (!wifi_started) {
        ESP_ERROR_CHECK(esp_wifi_start());
        wifi_started = true;
    }
}

static void net_task(void *arg)
{
    char ssid[33] = "";
    char pass[64] = "";
    bool have_credentials = nvs_read_str("wifi_ssid", ssid, sizeof(ssid)) && ssid[0] != '\0';
    if (have_credentials && !nvs_read_str("wifi_pass", pass, sizeof(pass))) {
        pass[0] = '\0';                     // open network
    }

    xSemaphoreTake(lock, portMAX_DELAY);
    memcpy(status.ssid, ssid, sizeof(status.ssid));
    xSemaphoreGive(lock);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    if (have_credentials) {
        wifi_config_t sta = {0};
        memcpy(sta.sta.ssid, ssid, strlen(ssid));
        memcpy(sta.sta.password, pass, strlen(pass));
        sta.sta.threshold.authmode = pass[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
        ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    }

#if CONFIG_MRROIP_ETHERNET
    // A cable at boot: give it its address before Wi-Fi starts at all
    xSemaphoreTake(lock, portMAX_DELAY);
    bool link = status.eth_link;
    xSemaphoreGive(lock);
    if (!link) {
        xEventGroupWaitBits(events, ETH_CHANGED_BIT, pdFALSE, pdFALSE, pdMS_TO_TICKS(ETH_LINK_WAIT_MS));
        xSemaphoreTake(lock, portMAX_DELAY);
        link = status.eth_link;
        xSemaphoreGive(lock);
    }
    if (link) {
        wait_eth_ready(ETH_DHCP_WAIT_MS);
    }
#endif

    while (1) {
        if (eth_ready(NULL)) {
            use_ethernet();                 // returns when the cable is gone
        }
        start_wifi();
        join_t joined = JOIN_FAILED;
        for (int attempt = 1; have_credentials && attempt <= NET_STA_ATTEMPTS && joined == JOIN_FAILED; attempt++) {
            set_state(NET_CONNECTING, attempt);
            joined = try_station(STA_ATTEMPT_TIMEOUT_MS);
            if (joined == JOIN_FAILED) {
                ESP_LOGW(TAG, "Attempt %d/%d to join '%s' failed", attempt, NET_STA_ATTEMPTS, ssid);
            }
        }
        if (joined == JOIN_CABLE) {
            continue;
        }
        if (joined == JOIN_FAILED) {
            break;                          // to the setup AP
        }
        xSemaphoreTake(lock, portMAX_DELAY);
        keep_connected = true;
        xSemaphoreGive(lock);
        ESP_LOGW(TAG, "Joined '%s', IP %s", ssid, status.ip);
#if CONFIG_MRROIP_ETHERNET
        while (!wait_eth_ready(60 * 60 * 1000)) {
        }
#else
        vTaskDelete(NULL);
#endif
    }

    start_setup_ap();

    // A layout often powers up all at once and the router can take longer than the three attempts.
    // So keep trying the stored network in the background, but only while nobody uses the setup AP:
    // a join attempt changes channel and would drop a phone that is filling in the form.
    // A cable with an address ends setup mode the same way.
    while (1) {
        xEventGroupClearBits(events, ETH_CHANGED_BIT);
        EventBits_t bits = xEventGroupWaitBits(events, GOT_IP_BIT | ETH_CHANGED_BIT, pdFALSE, pdFALSE,
                                               pdMS_TO_TICKS(AP_RETRY_INTERVAL_MS));
        if (eth_ready(NULL)) {
            ESP_LOGW(TAG, "Ethernet has an address; restarting to leave setup mode");
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_restart();
        }
        if (bits & ETH_CHANGED_BIT) {
            continue;
        }
        if (!(bits & GOT_IP_BIT) && have_credentials) {
            xSemaphoreTake(lock, portMAX_DELAY);
            int clients = status.ap_clients;
            xSemaphoreGive(lock);
            if (clients == 0 && try_station(STA_ATTEMPT_TIMEOUT_MS) == JOIN_OK) {
                bits = GOT_IP_BIT;
            }
        }
        if (bits & GOT_IP_BIT) {
            ESP_LOGW(TAG, "'%s' is reachable again; restarting to leave setup mode", ssid);
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_restart();
        }
    }
}

#if CONFIG_MRROIP_ETHERNET
// ESP32 EMAC + an RMII PHY driven as a generic IEEE 802.3 PHY (the IP101 works so), pins from mrroip_start().
// DHCP client by default.
static void ethernet_start(void)
{
    if (!eth_configured) {
        ESP_LOGE(TAG, "CONFIG_MRROIP_ETHERNET is on, but mrroip_config_t has no Ethernet pins; Wi-Fi only");
        return;
    }
    eth_mac_config_t mac_config = ETH_MAC_DEFAULT_CONFIG();
    eth_esp32_emac_config_t emac_config = ETH_ESP32_EMAC_DEFAULT_CONFIG();
    emac_config.smi_gpio.mdc_num = eth_pins.mdc_gpio;
    emac_config.smi_gpio.mdio_num = eth_pins.mdio_gpio;
    emac_config.interface = EMAC_DATA_INTERFACE_RMII;
    emac_config.clock_config.rmii.clock_mode = EMAC_CLK_EXT_IN;
    emac_config.clock_config.rmii.clock_gpio = eth_pins.ref_clk_gpio;
    eth_phy_config_t phy_config = ETH_PHY_DEFAULT_CONFIG();
    phy_config.phy_addr = ESP_ETH_PHY_ADDR_AUTO;
    phy_config.reset_gpio_num = eth_pins.phy_reset_gpio;

    esp_eth_mac_t *mac = esp_eth_mac_new_esp32(&emac_config, &mac_config);
    esp_eth_phy_t *phy = esp_eth_phy_new_generic(&phy_config);
    esp_eth_config_t config = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t handle = NULL;
    if (!mac || !phy || esp_eth_driver_install(&config, &handle) != ESP_OK) {
        ESP_LOGE(TAG, "Ethernet driver did not start; Wi-Fi only");
        return;
    }
    esp_netif_config_t netif_config = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&netif_config);
    ESP_ERROR_CHECK(esp_netif_attach(netif, esp_eth_new_netif_glue(handle)));
    ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_LOST_IP, on_event, NULL));
    eth_netif = netif;
    if (esp_eth_start(handle) != ESP_OK) {
        ESP_LOGE(TAG, "Ethernet did not start; Wi-Fi only");
    }
}
#endif

void net_init(const char *device_type, const mrroip_ethernet_pins_t *ethernet)
{
    lock = xSemaphoreCreateMutex();
    events = xEventGroupCreate();
    snprintf(device_type_name, sizeof(device_type_name), "%s", device_type);
#if CONFIG_MRROIP_ETHERNET
    if (ethernet) {
        eth_pins = *ethernet;
        eth_configured = true;
    }
#else
    (void)ethernet;
#endif

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&config));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));    // credentials live in "mrroip" only
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
}

void net_start(void)
{
#if CONFIG_MRROIP_ETHERNET
    ethernet_start();
#endif
    xTaskCreate(net_task, "net", 4 * 1024, NULL, 3, NULL);
}
