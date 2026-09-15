#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "cJSON.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "lwip/inet.h"
#include "control.h"
#include "discovery.h"
#include "http_api.h"
#include "mmroip_params.h"
#include "mmroip_profile.h"
#include "mmroip_value.h"

static const char *TAG = "control";

#define REPLAY_WINDOW_MS    5000        // §9.5
#define MASTERS_MAX         8
#define OBJECTS_MAX         8
#define TIMEOUT_CHECK_MS    100

typedef enum { AUTH_AUTONOMOUS, AUTH_COMMANDED, AUTH_IDLE } authority_t;
static const char *const authority_names[] = { "autonomous", "commanded", "idle" };
static const char *const core_modes[] = { "estop", "reset", "release", "hold", NULL };

typedef struct {
    bool used;
    uint32_t ip;
    double last_seq;
    int64_t last_ms;
} master_t;

static SemaphoreHandle_t lock;          // guards everything below and serialises every control message
static master_t masters[MASTERS_MAX];
static authority_t authority;
static uint32_t authority_ip;
static int64_t authority_ms;
static char mode[PARAM_STR_MAX];
static bool estop_latched;

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void ip_text(uint32_t ip, char *out, size_t size)
{
    struct in_addr addr = { .s_addr = ip };
    inet_ntoa_r(addr, out, (int)size);
}

static bool in_list(const char *const *list, const char *value)
{
    for (; list && *list; list++) {
        if (strcmp(*list, value) == 0) {
            return true;
        }
    }
    return false;
}

static authority_t rest_authority(void)
{
    return profile_info()->autonomous ? AUTH_AUTONOMOUS : AUTH_IDLE;
}

// The master is gone (timeout or release): dispatch on device_class (§11.3). Call with lock held.
static void master_lost(const char *why)
{
    const profile_info_t *info = profile_info();
    authority_t next = rest_authority();
    if (strcmp(info->device_class, "passive") != 0 && next != AUTH_AUTONOMOUS) {
        profile_come_to_rest();     // mobile stops; stationary without a programme comes to rest
    }                               // passive holds its last state; autonomous motion is never subject to it
    char ip[16];
    ip_text(authority_ip, ip, sizeof(ip));
    ESP_LOGW(TAG, "AUTHORITY %s -> %s (%s, master %s)", authority_names[authority], authority_names[next], why, ip);
    authority = next;
}

static void timeout_task(void *arg)
{
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(TIMEOUT_CHECK_MS));
        int32_t timeout_ms = params_get_int("control_timeout_ms");
        xSemaphoreTake(lock, portMAX_DELAY);
        if (authority == AUTH_COMMANDED && now_ms() - authority_ms > timeout_ms) {
            master_lost("timeout");     // armed only while commanded; /state polling never refreshes it
        }
        xSemaphoreGive(lock);
    }
}

void control_start(void)
{
    lock = xSemaphoreCreateMutex();
    authority = rest_authority();
    strlcpy(mode, profile_info()->rest_mode, sizeof(mode));
    xTaskCreate(timeout_task, "control", 3 * 1024, NULL, 4, NULL);
}

// Call with lock held
static cJSON *state_json_locked(void)
{
    bool busy = false;
    const char *fault = NULL;
    cJSON *state = cJSON_CreateObject();
    cJSON_AddStringToObject(state, "mode", mode);
    cJSON_AddStringToObject(state, "authority", authority_names[authority]);
    cJSON *profile = cJSON_CreateObject();
    profile_emit_state(profile, &busy, &fault);
    cJSON_AddBoolToObject(state, "busy", busy);
    if (fault) {
        cJSON_AddStringToObject(state, "fault", fault);
    } else {
        cJSON_AddNullToObject(state, "fault");
    }
    cJSON_AddNumberToObject(state, "uptime_ms", (double)now_ms());
    cJSON_AddItemToObject(state, "profile", profile);
    return state;
}

cJSON *control_state_json(void)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    cJSON *state = state_json_locked();
    xSemaphoreGive(lock);
    return state;
}

// The entry for a sender, or a fresh one (reusing the least recently accepted) if it has none
static master_t *master_for(uint32_t ip)
{
    master_t *oldest = &masters[0];
    for (size_t i = 0; i < MASTERS_MAX; i++) {
        if (masters[i].used && masters[i].ip == ip) {
            return &masters[i];
        }
        if (!masters[i].used || (oldest->used && masters[i].last_ms < oldest->last_ms)) {
            oldest = &masters[i];
        }
    }
    *oldest = (master_t) { .used = false, .ip = ip };
    return oldest;
}

static int print_reply(cJSON *reply, char *out, size_t outlen, int status)
{
    if (!cJSON_PrintPreallocated(reply, out, (int)outlen, false)) {
        strlcpy(out, "{\"accepted\":false,\"error\":\"response_too_large\"}", outlen);
    }
    cJSON_Delete(reply);
    return status;
}

void control_reject(const char *error, char *out, size_t outlen)
{
    cJSON *reply = cJSON_CreateObject();
    cJSON_AddStringToObject(reply, "device_id", http_api_device_id());
    cJSON_AddBoolToObject(reply, "accepted", false);
    cJSON_AddStringToObject(reply, "error", error);
    cJSON_AddItemToObject(reply, "state", control_state_json());
    print_reply(reply, out, outlen, 400);
}

int control_apply(const char *json, size_t len, uint32_t source_ip, char *out, size_t outlen)
{
    const profile_info_t *info = profile_info();
    discovery_note_traffic();
    cJSON *msg = cJSON_ParseWithLength(json, len);
    cJSON *reply = cJSON_CreateObject();
    cJSON *details = NULL;
    const char *error = NULL;
    const char *missing = NULL;
    int status = 400;
    cJSON_AddStringToObject(reply, "device_id", http_api_device_id());

    xSemaphoreTake(lock, portMAX_DELAY);

    if (!cJSON_IsObject(msg)) {
        error = "malformed_json";
        goto reject;
    }
    cJSON *seq = cJSON_GetObjectItemCaseSensitive(msg, "seq");
    cJSON *ts = cJSON_GetObjectItemCaseSensitive(msg, "ts");
    cJSON *mode_item = cJSON_GetObjectItemCaseSensitive(msg, "mode");
    cJSON *target = cJSON_GetObjectItemCaseSensitive(msg, "target");
    cJSON *objects = cJSON_GetObjectItemCaseSensitive(msg, "objects");
    bool hold = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(msg, "hold"));
    if (cJSON_IsNumber(seq)) {
        cJSON_AddNumberToObject(reply, "ack_seq", seq->valuedouble);
    }
    if (cJSON_IsNumber(ts)) {
        cJSON_AddNumberToObject(reply, "ts", ts->valuedouble);
    }

    if (!cJSON_IsNumber(seq)) {
        error = "missing_field";
        missing = "seq";
        goto reject;
    }
    if (!mode_item) {
        error = "missing_field";
        missing = "mode";
        goto reject;
    }
    const char *requested = cJSON_IsString(mode_item) ? mode_item->valuestring : "";
    bool is_core = in_list(core_modes, requested);
    bool is_profile = in_list(info->modes, requested);
    if (!is_core && !is_profile) {
        error = "unknown_mode";
        goto reject;
    }
    bool takes_target = in_list(info->target_modes, requested);
    if (target && !takes_target) {
        error = "unknown_target";
        goto reject;
    }
    if (!target && takes_target) {
        error = "missing_target";
        goto reject;
    }

    bool is_estop = (strcmp(requested, "estop") == 0);
    master_t *master = master_for(source_ip);
    int64_t now = now_ms();
    if (!is_estop && master->used && seq->valuedouble <= master->last_seq && now - master->last_ms <= REPLAY_WINDOW_MS) {
        error = "stale_seq";        // a reordered duplicate; after a longer gap the master restarted its counter
        status = 409;
        goto reject;
    }

    // Objects are desired states that go with a profile mode; check all of them before applying any
    const char *ids[OBJECTS_MAX];
    const object_value_t *values[OBJECTS_MAX];
    size_t count = 0;
    if (objects) {
        if (!is_profile || !cJSON_IsObject(objects)) {
            error = is_profile ? "invalid_object_state" : "unexpected_objects";
            goto reject;
        }
        details = cJSON_CreateArray();
        for (cJSON *o = objects->child; o; o = o->next) {
            const char *reason = count < OBJECTS_MAX
                                 ? profile_object_check(o->string, o)
                                 : "too_many_objects";
            if (reason) {
                cJSON *detail = cJSON_CreateObject();
                cJSON_AddStringToObject(detail, "key", o->string);
                cJSON_AddStringToObject(detail, "reason", reason);
                cJSON_AddItemToArray(details, detail);
                continue;
            }
            ids[count] = o->string;
            values[count] = o;
            count++;
        }
        if (cJSON_GetArraySize(details) > 0) {
            error = "invalid_object_state";
            goto reject;
        }
    }

    bool changes_state = is_profile && !hold;
    if (changes_state && estop_latched) {
        error = "latched_estop";
        status = 409;
        goto reject;
    }
    if (changes_state && profile_fault()) {
        error = "latched_fault";
        status = 409;
        goto reject;
    }

    // Accepted
    master->used = true;
    master->last_seq = seq->valuedouble;
    master->last_ms = now;
    if (authority == AUTH_COMMANDED && authority_ip != source_ip) {
        char previous[16];
        ip_text(authority_ip, previous, sizeof(previous));
        cJSON_AddStringToObject(reply, "authority_taken_from", previous);   // accepted, but visible (§11.2)
    }

    if (strcmp(requested, "release") == 0) {
        if (authority == AUTH_COMMANDED) {
            authority_ip = source_ip;
            master_lost("release");
        }
    } else {
        if (authority != AUTH_COMMANDED || authority_ip != source_ip) {
            char ip[16];
            ip_text(source_ip, ip, sizeof(ip));
            ESP_LOGW(TAG, "AUTHORITY %s -> commanded (master %s)", authority_names[authority], ip);
        }
        authority = AUTH_COMMANDED;
        authority_ip = source_ip;
        authority_ms = now;

        if (is_estop) {
            if (!estop_latched) {
                profile_estop();
                estop_latched = true;
                ESP_LOGW(TAG, "ESTOP latched");
            }
            strlcpy(mode, "estop", sizeof(mode));
        } else if (strcmp(requested, "reset") == 0) {
            profile_reset();
            if (estop_latched) {
                ESP_LOGW(TAG, "ESTOP cleared by reset");
            }
            estop_latched = false;
            strlcpy(mode, info->rest_mode, sizeof(mode));
        } else if (changes_state) {
            profile_apply(requested, count, ids, values);   // desired state: repeating it changes nothing
            strlcpy(mode, requested, sizeof(mode));
        }
        // "hold", or hold: true: authority refreshed, nothing else changes
    }
    cJSON_AddBoolToObject(reply, "accepted", true);
    status = 200;
    goto done;

reject:
    cJSON_AddBoolToObject(reply, "accepted", false);
    cJSON_AddStringToObject(reply, "error", error);
    if (missing) {
        cJSON_AddStringToObject(reply, "field", missing);
    }
    if (details && cJSON_GetArraySize(details) > 0) {
        cJSON_AddItemToObject(reply, "details", details);
        details = NULL;
    }

done:
    cJSON_AddItemToObject(reply, "state", state_json_locked());
    xSemaphoreGive(lock);
    cJSON_Delete(details);
    cJSON_Delete(msg);
    profile_message(PROFILE_MSG_CONTROL, status == 200);
    return print_reply(reply, out, outlen, status);
}
