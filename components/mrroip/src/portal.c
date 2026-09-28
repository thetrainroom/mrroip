#include <ctype.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"          // dns_server.h needs esp_ip4_addr_t
#include "esp_system.h"
#include "dns_server.h"
#include "discovery.h"
#include "net.h"
#include "portal.h"

static const char *TAG = "portal";

#define FORM_BODY_MAX       256
#define AP_URL              "http://192.168.4.1/"
#define RESTART_DELAY_MS    1000

static const char (*offered)[33];
static size_t offered_count;
static const char *page_title;

typedef struct {
    char ssid[33];
    char pass[64];
} credentials_t;

static const char PAGE_HEAD[] =
    "<!doctype html><html><head><meta charset=utf-8>"
    "<meta name=viewport content=\"width=device-width,initial-scale=1\"><title>";
static const char PAGE_BODY[] =
    "</title><style>body{font-family:sans-serif;margin:1.5em auto;max-width:24em;padding:0 1em}"
    "input,select,button{font-size:1em;width:100%;padding:.5em;margin:.3em 0 1em;box-sizing:border-box}</style>"
    "</head><body>";
static const char PAGE_TAIL[] = "</body></html>";

static void escape_html(const char *in, char *out, size_t size)
{
    size_t n = 0;
    for (; *in; in++) {
        const char *rep = NULL;
        switch (*in) {
        case '&':  rep = "&amp;";  break;
        case '<':  rep = "&lt;";   break;
        case '>':  rep = "&gt;";   break;
        case '"':  rep = "&quot;"; break;
        case '\'': rep = "&#39;";  break;
        }
        size_t len = rep ? strlen(rep) : 1;
        if (n + len >= size) {
            break;
        }
        if (rep) {
            memcpy(out + n, rep, len);
        } else {
            out[n] = *in;
        }
        n += len;
    }
    out[n] = '\0';
}

static void send_page_start(httpd_req_t *req)
{
    char title[64];
    escape_html(page_title, title, sizeof(title));
    httpd_resp_set_type(req, "text/html");
    httpd_resp_sendstr_chunk(req, PAGE_HEAD);
    httpd_resp_sendstr_chunk(req, title);
    httpd_resp_sendstr_chunk(req, PAGE_BODY);
}

static esp_err_t send_page_end(httpd_req_t *req)
{
    httpd_resp_sendstr_chunk(req, PAGE_TAIL);
    return httpd_resp_sendstr_chunk(req, NULL);
}

static esp_err_t form_get(httpd_req_t *req)
{
    char name[33 * 6];     // worst case: every character escaped as &quot;
    send_page_start(req);
    httpd_resp_sendstr_chunk(req,
        "<h1>Wi-Fi setup</h1><form method=post action=/>"
        "<label>Network<select onchange=\"document.getElementById('s').value=this.value\">"
        "<option value=\"\">choose…</option>");
    for (size_t i = 0; i < offered_count; i++) {
        escape_html(offered[i], name, sizeof(name));
        httpd_resp_sendstr_chunk(req, "<option value=\"");
        httpd_resp_sendstr_chunk(req, name);
        httpd_resp_sendstr_chunk(req, "\">");
        httpd_resp_sendstr_chunk(req, name);
        httpd_resp_sendstr_chunk(req, "</option>");
    }
    httpd_resp_sendstr_chunk(req,
        "</select></label>"
        "<label>or type its name<input id=s name=ssid maxlength=32 required></label>"
        "<label>Password<input name=pass type=password maxlength=63></label>"
        "<button>Save and restart</button></form>");
    return send_page_end(req);
}

static int hex_value(char c)
{
    return isdigit((unsigned char)c) ? c - '0' : tolower((unsigned char)c) - 'a' + 10;
}

// Decode the value of `key` from an application/x-www-form-urlencoded body.
// Returns 1 if found, 0 if absent, -1 if it does not fit or contains a NUL.
static int form_value(const char *body, const char *key, char *out, size_t size)
{
    size_t key_len = strlen(key);
    for (const char *p = body; p; p = strchr(p, '&') ? strchr(p, '&') + 1 : NULL) {
        if (strncmp(p, key, key_len) != 0 || p[key_len] != '=') {
            continue;
        }
        size_t n = 0;
        for (const char *v = p + key_len + 1; *v && *v != '&'; v++) {
            char c = *v;
            if (c == '+') {
                c = ' ';
            } else if (c == '%' && isxdigit((unsigned char)v[1]) && isxdigit((unsigned char)v[2])) {
                c = (char)(hex_value(v[1]) * 16 + hex_value(v[2]));
                v += 2;
            }
            if (c == '\0' || n + 1 >= size) {
                return -1;
            }
            out[n++] = c;
        }
        out[n] = '\0';
        return 1;
    }
    return 0;
}

// Flash is not written from the HTTP server's thread (§8.3): a short-lived task stores and restarts,
// which also gives the response time to reach the phone
static void save_and_restart_task(void *arg)
{
    credentials_t *credentials = arg;
    vTaskDelay(pdMS_TO_TICKS(RESTART_DELAY_MS));
    esp_err_t err = net_store_credentials(credentials->ssid, credentials->pass);
    free(credentials);
    if (err == ESP_OK) {
        discovery_byebye();
        esp_restart();
    }
    ESP_LOGE(TAG, "Saving the Wi-Fi credentials failed: %s", esp_err_to_name(err));
    vTaskDelete(NULL);
}

static esp_err_t form_post(httpd_req_t *req)
{
    if (req->content_len > FORM_BODY_MAX) {
        httpd_resp_set_status(req, "413 Payload Too Large");
        return httpd_resp_send(req, NULL, 0);
    }
    char body[FORM_BODY_MAX + 1];
    int received = 0;
    while (received < (int)req->content_len) {
        int r = httpd_req_recv(req, body + received, req->content_len - received);
        if (r == HTTPD_SOCK_ERR_TIMEOUT) {
            continue;
        }
        if (r <= 0) {
            return ESP_FAIL;
        }
        received += r;
    }
    body[received] = '\0';

    credentials_t *credentials = calloc(1, sizeof(*credentials));
    if (!credentials) {
        return httpd_resp_send_500(req);
    }
    int ssid_found = form_value(body, "ssid", credentials->ssid, sizeof(credentials->ssid));
    int pass_found = form_value(body, "pass", credentials->pass, sizeof(credentials->pass));
    if (ssid_found != 1 || pass_found < 0 || net_validate_credentials(credentials->ssid, credentials->pass) != ESP_OK) {
        free(credentials);
        httpd_resp_set_status(req, "400 Bad Request");
        send_page_start(req);
        httpd_resp_sendstr_chunk(req,
            "<h1>Not saved</h1><p>The network name must be 1–32 characters. The password must be "
            "8–63 characters, or empty for an open network.</p><p><a href=/>Back</a></p>");
        return send_page_end(req);
    }

    char name[33 * 6];
    escape_html(credentials->ssid, name, sizeof(name));
    send_page_start(req);
    httpd_resp_sendstr_chunk(req, "<h1>Saved</h1><p>The display restarts and joins <b>");
    httpd_resp_sendstr_chunk(req, name);
    httpd_resp_sendstr_chunk(req, "</b>. Its address appears on the display.</p>"
                                  "<p>You can switch this phone back to your normal Wi-Fi.</p>");
    esp_err_t err = send_page_end(req);

    if (xTaskCreate(save_and_restart_task, "save", 3 * 1024, credentials, 2, NULL) != pdPASS) {
        free(credentials);
    }
    return err;
}

esp_err_t portal_redirect(httpd_req_t *req)
{
    httpd_resp_set_status(req, "302 Found");
    httpd_resp_set_hdr(req, "Location", AP_URL);
    return httpd_resp_send(req, NULL, 0);
}

void portal_start(httpd_handle_t server, const char ssids[][33], size_t count, const char *ap_ssid)
{
    offered = ssids;
    offered_count = count;
    page_title = ap_ssid;

    if (!server) {
        ESP_LOGE(TAG, "No HTTP server; the setup form is unavailable");
    } else {
        static const httpd_uri_t get = { .uri = "/", .method = HTTP_GET, .handler = form_get };
        static const httpd_uri_t post = { .uri = "/", .method = HTTP_POST, .handler = form_post };
        httpd_register_uri_handler(server, &get);
        httpd_register_uri_handler(server, &post);
    }

    dns_server_config_t dns = DNS_SERVER_CONFIG_SINGLE("*", "WIFI_AP_DEF");   // every name -> the AP address
    start_dns_server(&dns);
}
