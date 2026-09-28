/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "cJSON.h"
#include "lwip/sockets.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "control.h"
#include "discovery.h"
#include "http_api.h"
#include "net.h"
#include "mrroip_params.h"
#include "portal.h"
#include "mrroip_profile.h"
#include "proto_name.h"
#include "store.h"

static const char *TAG = "http_api";

#define BODY_MAX            4096        // §2.3
#define RESTART_DELAY_MS    500
#define CONTROL_REPLY_MAX   1536
#define OBJECT_ID_MAX       32
#define STREAM_CHUNK        1460        // one TCP segment's worth
#define STREAM_TIMEOUTS_MAX 3           // receive timeouts in a row before an upload counts as broken

static httpd_handle_t server;
static char device_id[32];
static char etag_base[112];      // firmware, profile version and profile part; config_version is added per request

static const char *const core_modes[] = { "estop", "reset", "release", "hold" };

const char *http_api_device_id(void)
{
    if (!device_id[0]) {
        uint8_t mac[6];
        esp_read_mac(mac, ESP_MAC_WIFI_STA);    // the station MAC, never the AP one (§5.1)
        snprintf(device_id, sizeof(device_id), "%02x:%02x:%02x:%02x:%02x:%02x",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }
    return device_id;
}

httpd_handle_t http_api_server(void)
{
    return server;
}

static void restart_task(void *arg)
{
    bool factory_reset = (bool)(intptr_t)arg;
    vTaskDelay(pdMS_TO_TICKS(RESTART_DELAY_MS));    // let the response leave first
    if (factory_reset) {
        store_erase_all();
    }
    if (!store_flush(pdMS_TO_TICKS(5000))) {
        ESP_LOGE(TAG, "Flash writes did not finish before the restart");
    }
    discovery_byebye();     // a clean restart (§6.1)
    esp_restart();
}

void http_api_restart_after_reply(bool factory_reset)
{
    xTaskCreate(restart_task, "restart", 3 * 1024, (void *)(intptr_t)factory_reset, 5, NULL);
}

// Sends and deletes body. status NULL means 200.
static esp_err_t send_json(httpd_req_t *req, const char *status, cJSON *body)
{
    char *text = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    if (!text) {
        return httpd_resp_send_500(req);
    }
    if (status) {
        httpd_resp_set_status(req, status);
    }
    httpd_resp_set_type(req, "application/json");
    esp_err_t err = httpd_resp_sendstr(req, text);
    cJSON_free(text);
    return err;
}

static esp_err_t send_error(httpd_req_t *req, const char *status, const char *error)
{
    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "error", error);
    return send_json(req, status, body);
}

static esp_err_t definition_get(httpd_req_t *req)
{
    // /definition carries device_name and udp_control_port, so a config write must change the tag
    char etag[sizeof(etag_base) + 16];
    snprintf(etag, sizeof(etag), "\"%s-%" PRIu32 "\"", etag_base, params_config_version());
    char if_none_match[sizeof(etag)];
    if (httpd_req_get_hdr_value_str(req, "If-None-Match", if_none_match, sizeof(if_none_match)) == ESP_OK &&
        strcmp(if_none_match, etag) == 0) {
        httpd_resp_set_status(req, "304 Not Modified");
        httpd_resp_set_hdr(req, "ETag", etag);
        return httpd_resp_send(req, NULL, 0);
    }

    const profile_info_t *info = profile_info();
    char name[PARAM_STR_MAX];
    params_get_str("device_name", name, sizeof(name));

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "proto", MRROIP_NAME);
    cJSON_AddStringToObject(root, "proto_version", MRROIP_VERSION);
    cJSON_AddStringToObject(root, "device_id", http_api_device_id());
    cJSON_AddStringToObject(root, "device_name", name);
    cJSON_AddStringToObject(root, "device_type", info->device_type);
    cJSON_AddStringToObject(root, "device_class", info->device_class);
    cJSON_AddStringToObject(root, "profile_version", info->profile_version);
    cJSON_AddStringToObject(root, "firmware", esp_app_get_description()->version);

    cJSON *endpoints = cJSON_AddObjectToObject(root, "endpoints");
    cJSON_AddStringToObject(endpoints, "definition", "/definition");
    cJSON_AddStringToObject(endpoints, "config", "/config");
    cJSON_AddStringToObject(endpoints, "control", "/control");
    cJSON_AddStringToObject(endpoints, "state", "/state");
    cJSON_AddStringToObject(endpoints, "objects", "/objects/{id}");     // binary uploads (plan question 16)
    cJSON_AddNumberToObject(endpoints, "udp_control_port", params_get_int("udp_port"));

    cJSON *capabilities = cJSON_AddObjectToObject(root, "capabilities");
    cJSON_AddBoolToObject(capabilities, "autonomous", info->autonomous);
    cJSON_AddBoolToObject(capabilities, "commanded", true);
    cJSON_AddNumberToObject(capabilities, "telemetry_hz", info->telemetry_hz);
    cJSON *modes = cJSON_AddArrayToObject(capabilities, "modes");
    for (const char *const *mode = info->modes; mode && *mode; mode++) {
        cJSON_AddItemToArray(modes, cJSON_CreateString(*mode));
    }
    cJSON_AddItemToObject(capabilities, "core_modes", cJSON_CreateStringArray(core_modes, 4));

    profile_emit_objects(cJSON_AddArrayToObject(root, "objects"));
    params_emit_definition(cJSON_AddArrayToObject(root, "parameters"));

    httpd_resp_set_hdr(req, "ETag", etag);
    return send_json(req, NULL, root);
}

static esp_err_t config_get(httpd_req_t *req)
{
    discovery_note_traffic();
    return send_json(req, NULL, params_config_json(http_api_device_id()));
}

static esp_err_t config_post(httpd_req_t *req)
{
    if (req->content_len > BODY_MAX) {
        esp_err_t err = send_error(req, "413 Payload Too Large", "body_too_large");
        httpd_sess_trigger_close(req->handle, httpd_req_to_sockfd(req));   // the unread body is still on the socket
        return err;
    }
    char *body = malloc(req->content_len + 1);
    if (!body) {
        return httpd_resp_send_500(req);
    }
    size_t received = 0;
    while (received < req->content_len) {
        int r = httpd_req_recv(req, body + received, req->content_len - received);
        if (r == HTTPD_SOCK_ERR_TIMEOUT) {
            continue;
        }
        if (r <= 0) {
            free(body);
            return ESP_FAIL;
        }
        received += (size_t)r;
    }

    config_reply_t reply;
    params_config_apply(body, received, http_api_device_id(), &reply);
    free(body);

    const char *status = reply.result == CONFIG_OK ? NULL
                       : reply.result == CONFIG_CONFLICT ? "409 Conflict" : "400 Bad Request";
    esp_err_t err = send_json(req, status, reply.body);
    if (reply.restart || reply.factory_reset) {
        http_api_restart_after_reply(reply.factory_reset);  // side effects after the response (§8.2)
    }
    return err;
}

static esp_err_t state_get(httpd_req_t *req)
{
    cJSON *state = control_state_json();
    // Not in §9.4, and only here: mrroip_probe.py C-31 reads it from /state, while C-19 compares the
    // state of two control responses, where a changing heap figure would differ (plan §6 question 4)
    cJSON_AddNumberToObject(state, "free_heap", esp_get_free_heap_size());
    // Same reasoning (plan §6 question 12): how the device is reached right now
    net_status_t net;
    net_get_status(&net);
    cJSON_AddStringToObject(state, "network", net.wired ? "ethernet" : net.state == NET_CONNECTED ? "wifi" :
                                              net.state == NET_SETUP_AP ? "setup_ap" : "none");
    return send_json(req, NULL, state);
}

static esp_err_t control_post(httpd_req_t *req)
{
    char reply[CONTROL_REPLY_MAX];
    if (req->content_len > BODY_MAX) {
        control_reject("body_too_large", reply, sizeof(reply));
        httpd_resp_set_status(req, "413 Payload Too Large");
        httpd_resp_set_type(req, "application/json");
        esp_err_t err = httpd_resp_sendstr(req, reply);
        httpd_sess_trigger_close(req->handle, httpd_req_to_sockfd(req));   // the unread body is still on the socket
        return err;
    }
    char *body = malloc(req->content_len + 1);
    if (!body) {
        return httpd_resp_send_500(req);
    }
    size_t received = 0;
    while (received < req->content_len) {
        int r = httpd_req_recv(req, body + received, req->content_len - received);
        if (r == HTTPD_SOCK_ERR_TIMEOUT) {
            continue;
        }
        if (r <= 0) {
            free(body);
            return ESP_FAIL;
        }
        received += (size_t)r;
    }

    struct sockaddr_in peer = {0};
    socklen_t peer_len = sizeof(peer);
    getpeername(httpd_req_to_sockfd(req), (struct sockaddr *)&peer, &peer_len);

    int status = control_apply(body, received, peer.sin_addr.s_addr, reply, sizeof(reply));
    free(body);
    if (status == 400) {
        httpd_resp_set_status(req, "400 Bad Request");
    } else if (status == 409) {
        httpd_resp_set_status(req, "409 Conflict");
    }
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, reply);
}

// The query string as values for the profile: integers where they parse as integers, strings otherwise.
// The core names no parameter; what they mean is the profile's business.
static void add_query_values(httpd_req_t *req, cJSON *request)
{
    char query[160];
    if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK) {
        return;
    }
    char *save = NULL;
    for (char *pair = strtok_r(query, "&", &save); pair; pair = strtok_r(NULL, "&", &save)) {
        char *eq = strchr(pair, '=');
        if (!eq || eq == pair) {
            continue;
        }
        *eq = '\0';
        char *end = NULL;
        long number = strtol(eq + 1, &end, 10);
        if (eq[1] && end && *end == '\0') {
            cJSON_AddNumberToObject(request, pair, number);
        } else {
            cJSON_AddStringToObject(request, pair, eq + 1);
        }
    }
}

static esp_err_t send_control_reply(httpd_req_t *req, int status, const char *reply)
{
    if (status == 400) {
        httpd_resp_set_status(req, "400 Bad Request");
    } else if (status == 409) {
        httpd_resp_set_status(req, "409 Conflict");
    }
    httpd_resp_set_type(req, "application/json");
    return httpd_resp_sendstr(req, reply);
}

// PUT /objects/<id>: an object state as a binary body, streamed to the profile (plan question 16)
static esp_err_t object_put(httpd_req_t *req)
{
    char reply[CONTROL_REPLY_MAX];
    const char *path = req->uri + strlen("/objects/");
    size_t id_len = strcspn(path, "?");
    if (id_len == 0 || id_len >= OBJECT_ID_MAX) {
        return send_error(req, "404 Not Found", "not_found");
    }
    char id[OBJECT_ID_MAX];
    memcpy(id, path, id_len);
    id[id_len] = '\0';

    cJSON *request = cJSON_CreateObject();
    add_query_values(req, request);
    char base[40];
    if (httpd_req_get_hdr_value_str(req, MRROIP_HEADER "Base", base, sizeof(base)) == ESP_OK) {
        cJSON_AddStringToObject(request, "base", base);
    }
    char seq_text[32];
    double seq = 0;
    bool have_seq = false;
    if (httpd_req_get_hdr_value_str(req, MRROIP_HEADER "Seq", seq_text, sizeof(seq_text)) == ESP_OK) {
        char *end = NULL;
        seq = strtod(seq_text, &end);
        have_seq = seq_text[0] && end && *end == '\0';
    }

    struct sockaddr_in peer = {0};
    socklen_t peer_len = sizeof(peer);
    getpeername(httpd_req_to_sockfd(req), (struct sockaddr *)&peer, &peer_len);

    int status = control_stream_begin(id, request, have_seq, seq, req->content_len, peer.sin_addr.s_addr,
                                      reply, sizeof(reply));
    cJSON_Delete(request);
    if (status != 200) {
        esp_err_t err = send_control_reply(req, status, reply);
        if (req->content_len > 0) {
            httpd_sess_trigger_close(req->handle, httpd_req_to_sockfd(req));   // the unread body is still on the socket
        }
        return err;
    }

    char *chunk = malloc(STREAM_CHUNK);
    size_t received = 0;
    int timeouts = 0;
    bool reading = (chunk != NULL);
    while (reading && received < req->content_len) {
        size_t wanted = req->content_len - received < STREAM_CHUNK ? req->content_len - received : STREAM_CHUNK;
        int r = httpd_req_recv(req, chunk, wanted);
        if (r == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts < STREAM_TIMEOUTS_MAX) {
            continue;
        }
        if (r <= 0) {
            break;
        }
        timeouts = 0;
        received += (size_t)r;
        reading = profile_object_stream_data((const uint8_t *)chunk, (size_t)r);
    }
    free(chunk);

    bool complete = (received == req->content_len);
    status = control_stream_end(complete, have_seq, seq, reply, sizeof(reply));
    esp_err_t err = send_control_reply(req, status, reply);
    if (!complete) {
        httpd_sess_trigger_close(req->handle, httpd_req_to_sockfd(req));
    }
    return err;
}

static esp_err_t not_found(httpd_req_t *req, httpd_err_code_t error)
{
    net_status_t net;
    net_get_status(&net);
    if (net.state == NET_SETUP_AP) {
        return portal_redirect(req);    // phones probing for a captive portal
    }
    return send_error(req, "404 Not Found", "not_found");
}

static esp_err_t method_not_allowed(httpd_req_t *req, httpd_err_code_t error)
{
    return send_error(req, "405 Method Not Allowed", "method_not_allowed");
}

void http_api_start(void)
{
    const profile_info_t *info = profile_info();
    snprintf(etag_base, sizeof(etag_base), "%s-%s-%s", esp_app_get_description()->version, info->profile_version, profile_etag());

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 12;
    config.stack_size = 6 * 1024;
    config.lru_purge_enable = true;
    config.uri_match_fn = httpd_uri_match_wildcard;       // /objects/<id>
    if (httpd_start(&server, &config) != ESP_OK) {
        ESP_LOGE(TAG, "HTTP server did not start");
        return;
    }
    static const httpd_uri_t handlers[] = {
        { .uri = "/definition", .method = HTTP_GET,  .handler = definition_get },
        { .uri = "/config",     .method = HTTP_GET,  .handler = config_get },
        { .uri = "/config",     .method = HTTP_POST, .handler = config_post },
        { .uri = "/state",      .method = HTTP_GET,  .handler = state_get },
        { .uri = "/control",    .method = HTTP_POST, .handler = control_post },
        { .uri = "/objects/*",  .method = HTTP_PUT,  .handler = object_put },
    };
    for (size_t i = 0; i < sizeof(handlers) / sizeof(handlers[0]); i++) {
        httpd_register_uri_handler(server, &handlers[i]);
    }
    httpd_register_err_handler(server, HTTPD_404_NOT_FOUND, not_found);
    httpd_register_err_handler(server, HTTPD_405_METHOD_NOT_ALLOWED, method_not_allowed);
}
