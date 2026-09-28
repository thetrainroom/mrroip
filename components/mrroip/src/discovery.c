#include <ctype.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "lwip/sockets.h"
#include "sdkconfig.h"
#if CONFIG_MRROIP_MDNS
#include "mdns.h"
#endif
#include "discovery.h"
#include "http_api.h"
#include "net.h"
#include "mrroip_params.h"
#include "mrroip_profile.h"
#include "proto_name.h"

static const char *TAG = "discovery";

#define SSDP_GROUP              "239.255.255.250"
#define SSDP_PORT               1900
#define WHOIS_PORT              8266
#define SSDP_TTL                4
#define SSDP_MAX_AGE_S          600
#define STARTUP_REPEATS         3           // §6.1: multicast is lossy and the start-up announcement matters most
#define STARTUP_GAP_MS          100
#define ANNOUNCE_MIN_S          30
#define ANNOUNCE_DEFAULT_S      300
#define RECOVERY_MS             (30LL * 60 * 1000)
#define PENDING_MAX             4
#define PACKET_MAX              1024
#define IFACES_MAX              2           // station and setup AP
#define LOOP_MS                 100

typedef enum { MSG_ALIVE, MSG_BYEBYE, MSG_RESPONSE } msg_kind_t;

typedef struct {
    bool used;
    struct sockaddr_in to;
    int64_t due_ms;
} pending_t;

static int ssdp_sock = -1;
static int whois_sock = -1;
static pending_t pending[PENDING_MAX];          // M-SEARCH answers waiting for their random delay
static char joined[IFACES_MAX][16];             // interface addresses that joined the SSDP group
static portMUX_TYPE traffic_mux = portMUX_INITIALIZER_UNLOCKED;
static int64_t last_traffic_ms;
static volatile bool name_changed;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

void discovery_note_traffic(void)
{
    portENTER_CRITICAL(&traffic_mux);
    last_traffic_ms = now_ms();
    portEXIT_CRITICAL(&traffic_mux);
}

void discovery_name_changed(void)
{
    name_changed = true;
}

// Seconds between announcements; 0 while silenced. A master silences with announce_interval_s = 0, but
// once nobody has sent /control or /config for 30 minutes they resume at the default (§6.1).
static int effective_interval_s(void)
{
    int configured = params_get_int("announce_interval_s");
    if (configured > 0) {
        return configured < ANNOUNCE_MIN_S ? ANNOUNCE_MIN_S : configured;
    }
    portENTER_CRITICAL(&traffic_mux);
    int64_t quiet_ms = now_ms() - last_traffic_ms;
    portEXIT_CRITICAL(&traffic_mux);
    return quiet_ms >= RECOVERY_MS ? ANNOUNCE_DEFAULT_S : 0;
}

// The addresses the device has now: the station's while connected, the setup AP's while that runs
static int active_ips(char ips[IFACES_MAX][16])
{
    int n = 0;
    net_status_t net;
    net_get_status(&net);
    if (net.state == NET_CONNECTED && net.ip[0]) {
        strlcpy(ips[n++], net.ip, 16);
    }
    esp_netif_t *ap = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    esp_netif_ip_info_t info;
    if (net.state == NET_SETUP_AP && ap && esp_netif_get_ip_info(ap, &info) == ESP_OK && info.ip.addr) {
        esp_ip4addr_ntoa(&info.ip, ips[n++], 16);
    }
    return n;
}

// The device's address on the network facing `peer`, for LOCATION and whois
static bool ip_facing(uint32_t peer, char *out)
{
    esp_netif_t *ap = esp_netif_get_handle_from_ifkey("WIFI_AP_DEF");
    esp_netif_ip_info_t info;
    if (ap && esp_netif_get_ip_info(ap, &info) == ESP_OK && info.ip.addr &&
        ((peer ^ info.ip.addr) & info.netmask.addr) == 0) {
        esp_ip4addr_ntoa(&info.ip, out, 16);
        return true;
    }
    net_status_t net;
    net_get_status(&net);
    if (net.state == NET_CONNECTED && net.ip[0]) {
        strlcpy(out, net.ip, 16);
        return true;
    }
    return false;
}

// device_name is free text: never let a control character into an SSDP header
static void header_safe(const char *in, char *out, size_t size)
{
    size_t n = 0;
    for (; *in && n + 1 < size; in++) {
        out[n++] = (unsigned char)*in < 0x20 ? '?' : *in;
    }
    out[n] = '\0';
}

static int build_message(char *buf, size_t size, msg_kind_t kind, const char *ip)
{
    const profile_info_t *info = profile_info();
    const char *id = http_api_device_id();
    char name[PARAM_STR_MAX];
    char safe_name[PARAM_STR_MAX];
    params_get_str("device_name", name, sizeof(name));
    header_safe(name, safe_name, sizeof(safe_name));

    char mac[13] = {0};
    for (int i = 0, j = 0; id[i] && j < 12; i++) {
        if (id[i] != ':') {
            mac[j++] = id[i];
        }
    }
    char usn[128];
    snprintf(usn, sizeof(usn), "uuid:%s-%s::%s", MRROIP_TOKEN, mac, MRROIP_SSDP_ST);

    int len;
    if (kind == MSG_BYEBYE) {
        len = snprintf(buf, size,
                       "NOTIFY * HTTP/1.1\r\nHOST: %s:%d\r\nNT: %s\r\nNTS: ssdp:byebye\r\nUSN: %s\r\n\r\n",
                       SSDP_GROUP, SSDP_PORT, MRROIP_SSDP_ST, usn);
    } else {
        const char *idf = esp_get_idf_version();
        if (idf[0] == 'v') {
            idf++;
        }
        char start[64];
        if (kind == MSG_ALIVE) {
            snprintf(start, sizeof(start), "NOTIFY * HTTP/1.1\r\nHOST: %s:%d\r\n", SSDP_GROUP, SSDP_PORT);
        } else {
            snprintf(start, sizeof(start), "HTTP/1.1 200 OK\r\nEXT:\r\n");
        }
        len = snprintf(buf, size,
                       "%sCACHE-CONTROL: max-age=%d\r\n"
                       "LOCATION: http://%s/definition\r\n"
                       "%s: %s\r\n"
                       "%s"
                       "USN: %s\r\n"
                       "SERVER: esp-idf/%s %s/%s\r\n"
                       MRROIP_HEADER "ID: %s\r\n"
                       MRROIP_HEADER "NAME: %s\r\n"
                       MRROIP_HEADER "TYPE: %s\r\n"
                       MRROIP_HEADER "CLASS: %s\r\n\r\n",
                       start, SSDP_MAX_AGE_S, ip,
                       kind == MSG_ALIVE ? "NT" : "ST", MRROIP_SSDP_ST,
                       kind == MSG_ALIVE ? "NTS: ssdp:alive\r\n" : "",
                       usn, idf, MRROIP_NAME, MRROIP_VERSION,
                       id, safe_name, info->device_type, info->device_class);
    }
    return len < (int)size ? len : (int)size - 1;
}

static void send_on(const char *ip, msg_kind_t kind, int repeats)
{
    char buf[PACKET_MAX];
    int len = build_message(buf, sizeof(buf), kind, ip);
    struct in_addr iface = { .s_addr = inet_addr(ip) };
    setsockopt(ssdp_sock, IPPROTO_IP, IP_MULTICAST_IF, &iface, sizeof(iface));
    const struct sockaddr_in group = {
        .sin_family = AF_INET,
        .sin_port = htons(SSDP_PORT),
        .sin_addr.s_addr = inet_addr(SSDP_GROUP),
    };
    for (int i = 0; i < repeats; i++) {
        if (i > 0) {
            vTaskDelay(pdMS_TO_TICKS(STARTUP_GAP_MS));
        }
        sendto(ssdp_sock, buf, len, 0, (struct sockaddr *)&group, sizeof(group));
    }
}

// Join the SSDP group on interfaces that came up (announcing there), leave those that went away
static void update_memberships(bool announce)
{
    char ips[IFACES_MAX][16];
    int n = active_ips(ips);
    for (int j = 0; j < IFACES_MAX; j++) {
        bool still_up = false;
        for (int i = 0; i < n && joined[j][0]; i++) {
            still_up |= (strcmp(ips[i], joined[j]) == 0);
        }
        if (joined[j][0] && !still_up) {
            struct ip_mreq leave = { .imr_multiaddr.s_addr = inet_addr(SSDP_GROUP), .imr_interface.s_addr = inet_addr(joined[j]) };
            setsockopt(ssdp_sock, IPPROTO_IP, IP_DROP_MEMBERSHIP, &leave, sizeof(leave));
            joined[j][0] = '\0';
        }
    }
    for (int i = 0; i < n; i++) {
        int free_slot = -1;
        bool known = false;
        for (int j = 0; j < IFACES_MAX; j++) {
            known |= (strcmp(ips[i], joined[j]) == 0);
            if (!joined[j][0] && free_slot < 0) {
                free_slot = j;
            }
        }
        if (known || free_slot < 0) {
            continue;
        }
        struct ip_mreq join = { .imr_multiaddr.s_addr = inet_addr(SSDP_GROUP), .imr_interface.s_addr = inet_addr(ips[i]) };
        if (setsockopt(ssdp_sock, IPPROTO_IP, IP_ADD_MEMBERSHIP, &join, sizeof(join)) != 0) {
            ESP_LOGE(TAG, "Joining the SSDP group on %s failed: errno %d", ips[i], errno);
            continue;
        }
        strlcpy(joined[free_slot], ips[i], sizeof(joined[free_slot]));
        ESP_LOGW(TAG, "SSDP on %s%s", ips[i], announce ? ", announcing" : ", silenced");
        if (announce) {
            send_on(ips[i], MSG_ALIVE, STARTUP_REPEATS);
        }
    }
}

// Case-insensitive header lookup in an HTTP-over-UDP message
static bool header_value(const char *msg, const char *name, char *out, size_t size)
{
    size_t name_len = strlen(name);
    for (const char *line = strstr(msg, "\r\n"); line; line = strstr(line + 2, "\r\n")) {
        const char *p = line + 2;
        if (strncasecmp(p, name, name_len) == 0 && p[name_len] == ':') {
            p += name_len + 1;
            while (*p == ' ' || *p == '\t') {
                p++;
            }
            size_t n = strcspn(p, "\r\n");
            if (n >= size) {
                n = size - 1;
            }
            memcpy(out, p, n);
            out[n] = '\0';
            return true;
        }
    }
    return false;
}

static void handle_ssdp(int interval_s)
{
    char msg[PACKET_MAX + 1];
    struct sockaddr_in from;
    socklen_t from_len = sizeof(from);
    int n = recvfrom(ssdp_sock, msg, PACKET_MAX, 0, (struct sockaddr *)&from, &from_len);
    if (n <= 0) {
        return;
    }
    msg[n] = '\0';
    // Silenced means silent: mrroip_probe.py C-28 counts an M-SEARCH answer as an announcement
    if (interval_s == 0 || strncmp(msg, "M-SEARCH * HTTP/1.1\r\n", 21) != 0) {
        return;
    }
    char man[40], st[128], mx[8];
    if (!header_value(msg, "MAN", man, sizeof(man)) || !strstr(man, "ssdp:discover") ||
        !header_value(msg, "ST", st, sizeof(st)) ||
        (strcmp(st, "ssdp:all") != 0 && strcmp(st, MRROIP_SSDP_ST) != 0)) {
        return;
    }
    int max_wait_s = header_value(msg, "MX", mx, sizeof(mx)) ? atoi(mx) : 1;
    max_wait_s = max_wait_s < 1 ? 1 : max_wait_s > 5 ? 5 : max_wait_s;

    // One answer per searcher; repeated searches from the same socket reuse its slot
    pending_t *slot = NULL;
    for (int i = 0; i < PENDING_MAX; i++) {
        bool same = pending[i].used && pending[i].to.sin_addr.s_addr == from.sin_addr.s_addr &&
                    pending[i].to.sin_port == from.sin_port;
        if (same) {
            return;
        }
        if (!pending[i].used && !slot) {
            slot = &pending[i];
        }
    }
    if (slot) {
        // A random delay of 0…MX seconds, as SSDP requires, so a layout full of endpoints does not answer at once
        *slot = (pending_t) { .used = true, .to = from, .due_ms = now_ms() + esp_random() % (max_wait_s * 1000 + 1) };
    }
}

static void send_due_answers(void)
{
    int64_t now = now_ms();
    for (int i = 0; i < PENDING_MAX; i++) {
        if (!pending[i].used || now < pending[i].due_ms) {
            continue;
        }
        char ip[16];
        if (ip_facing(pending[i].to.sin_addr.s_addr, ip)) {
            char buf[PACKET_MAX];
            int len = build_message(buf, sizeof(buf), MSG_RESPONSE, ip);
            sendto(ssdp_sock, buf, len, 0, (struct sockaddr *)&pending[i].to, sizeof(pending[i].to));
        }
        pending[i].used = false;
    }
}

// §6.3: a datagram containing "whois" gets the identity back, so a blocked multicast can be told
// apart from a missing device
static void handle_whois(void)
{
    char msg[256];
    struct sockaddr_in from;
    socklen_t from_len = sizeof(from);
    int n = recvfrom(whois_sock, msg, sizeof(msg) - 1, 0, (struct sockaddr *)&from, &from_len);
    if (n <= 0) {
        return;
    }
    msg[n] = '\0';
    char ip[16];
    if (!strstr(msg, "whois") || !ip_facing(from.sin_addr.s_addr, ip)) {
        return;
    }
    const profile_info_t *info = profile_info();
    char name[PARAM_STR_MAX];
    params_get_str("device_name", name, sizeof(name));
    cJSON *reply = cJSON_CreateObject();
    cJSON_AddStringToObject(reply, "proto", MRROIP_NAME);
    cJSON_AddStringToObject(reply, "v", MRROIP_VERSION);
    cJSON_AddStringToObject(reply, "id", http_api_device_id());
    cJSON_AddStringToObject(reply, "name", name);
    cJSON_AddStringToObject(reply, "type", info->device_type);
    cJSON_AddStringToObject(reply, "class", info->device_class);
    cJSON_AddStringToObject(reply, "ip", ip);
    cJSON_AddStringToObject(reply, "definition", "/definition");
    char out[512];
    if (cJSON_PrintPreallocated(reply, out, sizeof(out), false)) {
        sendto(whois_sock, out, strlen(out), 0, (struct sockaddr *)&from, from_len);
    }
    cJSON_Delete(reply);
}

#if CONFIG_MRROIP_MDNS
// A usable host label from free text: lowercase letters, digits and single hyphens
static void hostname_from(const char *name, char *out, size_t size)
{
    size_t n = 0;
    for (; *name && n + 1 < size; name++) {
        char c = (char)tolower((unsigned char)*name);
        if (isalnum((unsigned char)c)) {
            out[n++] = c;
        } else if (n > 0 && out[n - 1] != '-') {
            out[n++] = '-';
        }
    }
    while (n > 0 && out[n - 1] == '-') {
        n--;
    }
    out[n] = '\0';
    if (n == 0) {
        strlcpy(out, profile_info()->device_type, size);
    }
}

// §6.2: a convenience, so a person can type <device_name>.local; nothing in the protocol depends on it
static void mdns_register(bool first)
{
    const profile_info_t *info = profile_info();
    char name[PARAM_STR_MAX];
    char host[PARAM_STR_MAX];
    params_get_str("device_name", name, sizeof(name));
    hostname_from(name, host, sizeof(host));
    if (first && mdns_init() != ESP_OK) {
        ESP_LOGE(TAG, "mDNS did not start");
        return;
    }
    mdns_hostname_set(host);
    mdns_instance_name_set(name);
    mdns_txt_item_t txt[] = {
        { "id", http_api_device_id() },
        { "name", name },
        { "type", info->device_type },
        { "class", info->device_class },
        { "fw", esp_app_get_description()->version },
    };
    if (!first) {
        mdns_service_remove(MRROIP_MDNS_SVC, "_tcp");
    }
    if (mdns_service_add(name, MRROIP_MDNS_SVC, "_tcp", 80, txt, sizeof(txt) / sizeof(txt[0])) != ESP_OK) {
        ESP_LOGE(TAG, "Adding the mDNS service failed");
    }
}
#endif

static int open_udp(int port, bool reuse)
{
    int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (sock < 0) {
        return -1;
    }
    int one = 1;
    if (reuse) {
        setsockopt(sock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    }
    const struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons((uint16_t)port),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(sock);
        return -1;
    }
    return sock;
}

static void discovery_task(void *arg)
{
    ssdp_sock = open_udp(SSDP_PORT, true);
    whois_sock = open_udp(WHOIS_PORT, false);
    if (ssdp_sock < 0 || whois_sock < 0) {
        ESP_LOGE(TAG, "Discovery sockets could not be opened: errno %d", errno);
        vTaskDelete(NULL);
    }
    uint8_t ttl = SSDP_TTL;
    setsockopt(ssdp_sock, IPPROTO_IP, IP_MULTICAST_TTL, &ttl, sizeof(ttl));
#if CONFIG_MRROIP_MDNS
    mdns_register(true);
#endif

    int max_fd = ssdp_sock > whois_sock ? ssdp_sock : whois_sock;
    int last_interval = -1;
    int64_t next_announce_ms = 0;
    int64_t next_iface_check_ms = 0;
    while (1) {
        int interval = effective_interval_s();
        if (interval != last_interval) {
            if (last_interval > 0 && interval == 0) {
                ESP_LOGW(TAG, "SSDP silenced (announce_interval_s = 0)");
            } else if (last_interval == 0 && interval > 0) {
                ESP_LOGW(TAG, "SSDP announcing again, every %d s", interval);
            }
            next_announce_ms = (last_interval == 0) ? 0 : now_ms() + interval * 1000LL;
            last_interval = interval;
        }

        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(ssdp_sock, &fds);
        FD_SET(whois_sock, &fds);
        struct timeval timeout = { .tv_sec = 0, .tv_usec = LOOP_MS * 1000 };
        if (select(max_fd + 1, &fds, NULL, NULL, &timeout) > 0) {
            if (FD_ISSET(ssdp_sock, &fds)) {
                handle_ssdp(interval);
            }
            if (FD_ISSET(whois_sock, &fds)) {
                handle_whois();
            }
        }
        send_due_answers();

        int64_t now = now_ms();
        if (now >= next_iface_check_ms) {
            update_memberships(interval > 0);       // a new interface gets the three start-up announcements
            next_iface_check_ms = now + 1000;
        }
        if (name_changed) {
            name_changed = false;
#if CONFIG_MRROIP_MDNS
            mdns_register(false);
#endif
            next_announce_ms = 0;                   // re-announce under the new name
        }
        if (interval > 0 && now >= next_announce_ms) {
            char ips[IFACES_MAX][16];
            int n = active_ips(ips);
            for (int i = 0; i < n; i++) {
                send_on(ips[i], MSG_ALIVE, 1);
            }
            next_announce_ms = now + interval * 1000LL;
        }
    }
}

void discovery_start(void)
{
    discovery_note_traffic();       // the recovery window counts from start-up
    xTaskCreate(discovery_task, "discovery", 4 * 1024, NULL, 3, NULL);
}

void discovery_byebye(void)
{
    if (ssdp_sock < 0) {
        return;
    }
    char ips[IFACES_MAX][16];
    int n = active_ips(ips);
    for (int i = 0; i < n; i++) {
        send_on(ips[i], MSG_BYEBYE, 1);
    }
}
