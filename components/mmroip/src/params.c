#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "cJSON.h"
#include "esp_log.h"
#include "nvs.h"
#include "mmroip_params.h"
#include "discovery.h"
#include "mmroip_profile.h"
#include "proto_name.h"
#include "store.h"

static const char *TAG = "params";

#define PARAMS_MAX              16
#define NVS_KEY_CONFIG_VERSION  "cfg_ver"
#define NVS_KEY_MAX             15

static const param_desc_t core_params[] = {
    { .name = "device_name", .type = PARAM_STRING, .max_len = 31, .doc = "Human label. Not an identifier." },
    { .name = "control_timeout_ms", .nvs_key = "ctrl_timeout_ms", .type = PARAM_INT,
      .default_int = 2000, .min = 500, .max = 30000, .unit = "ms" },
    { .name = "udp_port", .type = PARAM_INT, .default_int = MMROIP_UDP_PORT, .min = 1024, .max = 65535 },
    { .name = "announce_interval_s", .nvs_key = "announce_int_s", .type = PARAM_INT,
      .default_int = 300, .min = 0, .max = 86400, .unit = "s",
      .doc = "0 disables SSDP announcements; see rate-limit recovery." },
};

typedef struct {
    int32_t i;
    char s[PARAM_STR_MAX];
} value_t;

static SemaphoreHandle_t lock;
static const param_desc_t *table[PARAMS_MAX];
static size_t count;
static value_t running[PARAMS_MAX];     // effective values (RAM)
static value_t stored[PARAMS_MAX];      // what flash holds, or the default
static value_t booted[PARAMS_MAX];      // values at start: what restart-applied parameters are running with
static uint32_t config_version;

static const char *nvs_key(const param_desc_t *p)
{
    return p->nvs_key ? p->nvs_key : p->name;
}

static const char *default_str(const param_desc_t *p)
{
    return p->default_str ? p->default_str : profile_info()->device_type;
}

static int find(const char *name)
{
    for (size_t i = 0; i < count; i++) {
        if (strcmp(table[i]->name, name) == 0) {
            return (int)i;
        }
    }
    return -1;
}

static bool in_values(const param_desc_t *p, const char *s)
{
    if (!p->values) {
        return true;
    }
    for (const char *const *v = p->values; *v; v++) {
        if (strcmp(*v, s) == 0) {
            return true;
        }
    }
    return false;
}

static bool valid(const param_desc_t *p, const value_t *v)
{
    if (p->type == PARAM_INT) {
        return v->i >= p->min && v->i <= p->max;
    }
    return strlen(v->s) <= p->max_len && in_values(p, v->s);
}

static bool differs(const param_desc_t *p, const value_t *a, const value_t *b)
{
    return p->type == PARAM_INT ? a->i != b->i : strcmp(a->s, b->s) != 0;
}

void params_init(void)
{
    lock = xSemaphoreCreateMutex();
    for (size_t i = 0; i < sizeof(core_params) / sizeof(core_params[0]); i++) {
        table[count++] = &core_params[i];
    }
    size_t profile_count = 0;
    const param_desc_t *profile = profile_params(&profile_count);
    for (size_t i = 0; i < profile_count; i++) {
        assert(count < PARAMS_MAX);
        table[count++] = &profile[i];
    }

    nvs_handle_t nvs;
    bool have_nvs = (nvs_open(MMROIP_TOKEN, NVS_READONLY, &nvs) == ESP_OK);
    for (size_t i = 0; i < count; i++) {
        const param_desc_t *p = table[i];
        assert(strlen(nvs_key(p)) <= NVS_KEY_MAX && p->max_len < PARAM_STR_MAX);

        stored[i].i = p->default_int;
        strlcpy(stored[i].s, p->type == PARAM_STRING ? default_str(p) : "", sizeof(stored[i].s));

        value_t loaded = stored[i];
        bool found = false;
        if (have_nvs && p->type == PARAM_INT) {
            found = (nvs_get_i32(nvs, nvs_key(p), &loaded.i) == ESP_OK);
        } else if (have_nvs) {
            size_t len = sizeof(loaded.s);
            found = (nvs_get_str(nvs, nvs_key(p), loaded.s, &len) == ESP_OK);
        }
        if (found && valid(p, &loaded)) {
            stored[i] = loaded;
        } else if (found) {
            ESP_LOGW(TAG, "Stored %s is not valid, using the default", p->name);
        }
        running[i] = booted[i] = stored[i];
    }
    if (have_nvs) {
        nvs_get_u32(nvs, NVS_KEY_CONFIG_VERSION, &config_version);
        nvs_close(nvs);
    }
}

int32_t params_get_int(const char *name)
{
    int i = find(name);
    assert(i >= 0 && table[i]->type == PARAM_INT);
    xSemaphoreTake(lock, portMAX_DELAY);
    int32_t value = running[i].i;
    xSemaphoreGive(lock);
    return value;
}

void params_get_str(const char *name, char *out, size_t size)
{
    int i = find(name);
    assert(i >= 0 && table[i]->type == PARAM_STRING);
    xSemaphoreTake(lock, portMAX_DELAY);
    strlcpy(out, running[i].s, size);
    xSemaphoreGive(lock);
}

void params_emit_definition(cJSON *array)
{
    for (size_t i = 0; i < count; i++) {
        const param_desc_t *p = table[i];
        cJSON *entry = cJSON_CreateObject();
        cJSON_AddStringToObject(entry, "name", p->name);
        if (p->type == PARAM_INT) {
            cJSON_AddStringToObject(entry, "type", "int");
            cJSON_AddNumberToObject(entry, "default", p->default_int);
            cJSON_AddNumberToObject(entry, "min", p->min);
            cJSON_AddNumberToObject(entry, "max", p->max);
        } else {
            cJSON_AddStringToObject(entry, "type", "string");
            cJSON_AddStringToObject(entry, "default", default_str(p));
            cJSON_AddNumberToObject(entry, "max_len", p->max_len);
            if (p->values) {
                cJSON *values = cJSON_AddArrayToObject(entry, "values");
                for (const char *const *v = p->values; *v; v++) {
                    cJSON_AddItemToArray(values, cJSON_CreateString(*v));
                }
            }
        }
        if (p->unit) {
            cJSON_AddStringToObject(entry, "unit", p->unit);
        }
        cJSON_AddBoolToObject(entry, "persist", true);
        if (p->applies_at_restart) {
            cJSON_AddStringToObject(entry, "applies", "restart");   // proposal, MMROIP-PLAN.md §6 question 8
        }
        if (p->doc) {
            cJSON_AddStringToObject(entry, "doc", p->doc);
        }
        cJSON_AddItemToArray(array, entry);
    }
}

// Call with lock held
static cJSON *config_json_locked(const char *device_id)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "device_id", device_id);
    cJSON *config = cJSON_AddObjectToObject(root, "config");
    cJSON *dirty_keys = cJSON_CreateArray();
    cJSON *restart_keys = cJSON_CreateArray();
    for (size_t i = 0; i < count; i++) {
        const param_desc_t *p = table[i];
        if (p->type == PARAM_INT) {
            cJSON_AddNumberToObject(config, p->name, running[i].i);
        } else {
            cJSON_AddStringToObject(config, p->name, running[i].s);
        }
        if (differs(p, &running[i], &stored[i])) {
            cJSON_AddItemToArray(dirty_keys, cJSON_CreateString(p->name));
        }
        if (p->applies_at_restart && differs(p, &running[i], &booted[i])) {
            cJSON_AddItemToArray(restart_keys, cJSON_CreateString(p->name));
        }
    }
    cJSON *meta = cJSON_AddObjectToObject(root, "_meta");
    cJSON_AddBoolToObject(meta, "dirty", cJSON_GetArraySize(dirty_keys) > 0);
    cJSON_AddItemToObject(meta, "dirty_keys", dirty_keys);
    cJSON_AddNumberToObject(meta, "config_version", config_version);
    cJSON_AddItemToObject(meta, "restart_pending_keys", restart_keys);   // not in §8.1; see §6 question 8
    return root;
}

cJSON *params_config_json(const char *device_id)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    cJSON *root = config_json_locked(device_id);
    xSemaphoreGive(lock);
    return root;
}

static void add_detail(cJSON *details, const char *key, const char *reason, const param_desc_t *p)
{
    cJSON *detail = cJSON_CreateObject();
    cJSON_AddStringToObject(detail, "key", key);
    cJSON_AddStringToObject(detail, "reason", reason);
    if (p && strcmp(reason, "out_of_range") == 0) {
        cJSON_AddNumberToObject(detail, "min", p->min);
        cJSON_AddNumberToObject(detail, "max", p->max);
    } else if (p && strcmp(reason, "too_long") == 0) {
        cJSON_AddNumberToObject(detail, "max_len", p->max_len);
    } else if (p && strcmp(reason, "not_allowed") == 0 && p->values) {
        cJSON *values = cJSON_AddArrayToObject(detail, "values");
        for (const char *const *v = p->values; *v; v++) {
            cJSON_AddItemToArray(values, cJSON_CreateString(*v));
        }
    }
    cJSON_AddItemToArray(details, detail);
}

static cJSON *error_body(const char *error)
{
    cJSON *body = cJSON_CreateObject();
    cJSON_AddStringToObject(body, "error", error);
    return body;
}

void params_config_apply(const char *json, size_t len, const char *device_id, config_reply_t *reply)
{
    discovery_note_traffic();
    memset(reply, 0, sizeof(*reply));
    cJSON *root = cJSON_ParseWithLength(json, len);
    if (!cJSON_IsObject(root)) {
        cJSON_Delete(root);
        reply->result = CONFIG_BAD_REQUEST;
        reply->body = error_body("malformed_json");
        profile_message(PROFILE_MSG_CONFIG, false);
        return;
    }

    // Validate every key before touching anything
    cJSON *details = cJSON_CreateArray();
    value_t pending[PARAMS_MAX];
    uint32_t written = 0;
    bool persist = false;
    bool factory_reset = false;
    bool have_if_version = false;
    double if_version = 0;

    for (cJSON *item = root->child; item; item = item->next) {
        const char *key = item->string;
        if (strcmp(key, "persist") == 0 || strcmp(key, "factory_reset") == 0) {
            if (!cJSON_IsBool(item)) {
                add_detail(details, key, "wrong_type", NULL);
            } else if (key[0] == 'p') {
                persist = cJSON_IsTrue(item);
            } else {
                factory_reset = cJSON_IsTrue(item);
            }
            continue;
        }
        if (strcmp(key, "if_version") == 0) {
            if (!cJSON_IsNumber(item)) {
                add_detail(details, key, "wrong_type", NULL);
            } else {
                have_if_version = true;
                if_version = item->valuedouble;
            }
            continue;
        }
        int i = find(key);
        if (i < 0) {
            add_detail(details, key, "unknown_key", NULL);
            continue;
        }
        const param_desc_t *p = table[i];
        value_t value = {0};
        if (p->type == PARAM_INT) {
            if (!cJSON_IsNumber(item) || item->valuedouble != floor(item->valuedouble)) {
                add_detail(details, key, "wrong_type", NULL);
                continue;
            }
            if (item->valuedouble < p->min || item->valuedouble > p->max) {
                add_detail(details, key, "out_of_range", p);
                continue;
            }
            value.i = (int32_t)item->valuedouble;
        } else {
            if (!cJSON_IsString(item)) {
                add_detail(details, key, "wrong_type", NULL);
                continue;
            }
            if (strlen(item->valuestring) > p->max_len) {
                add_detail(details, key, "too_long", p);
                continue;
            }
            if (!in_values(p, item->valuestring)) {
                add_detail(details, key, "not_allowed", p);
                continue;
            }
            strlcpy(value.s, item->valuestring, sizeof(value.s));
        }
        pending[i] = value;
        written |= 1u << i;
    }
    for (size_t i = 0; i < count; i++) {
        if ((written & (1u << i)) && table[i]->applies_at_restart && !persist) {
            add_detail(details, table[i]->name, "requires_persist", NULL);  // a RAM-only value could never take effect
        }
    }
    cJSON_Delete(root);

    uint32_t changed_now = 0;
    xSemaphoreTake(lock, portMAX_DELAY);
    if (have_if_version && if_version != (double)config_version) {
        reply->result = CONFIG_CONFLICT;
        reply->body = config_json_locked(device_id);
        cJSON_AddStringToObject(reply->body, "error", "version_conflict");
    } else if (cJSON_GetArraySize(details) > 0) {
        reply->result = CONFIG_BAD_REQUEST;
        reply->body = error_body("validation_failed");
        cJSON_AddBoolToObject(reply->body, "applied", false);
        cJSON_AddItemToObject(reply->body, "details", details);
        details = NULL;
    } else if (factory_reset) {
        reply->result = CONFIG_OK;
        reply->factory_reset = true;
        reply->body = config_json_locked(device_id);
    } else {
        for (size_t i = 0; i < count; i++) {
            if (!(written & (1u << i))) {
                continue;
            }
            const param_desc_t *p = table[i];
            if (differs(p, &running[i], &pending[i]) && !p->applies_at_restart) {
                changed_now |= 1u << i;
            }
            running[i] = pending[i];
            if (persist) {
                stored[i] = pending[i];
                if (p->type == PARAM_INT) {
                    store_set_i32(nvs_key(p), pending[i].i);
                } else {
                    store_set_str(nvs_key(p), pending[i].s);
                }
            }
            if (p->applies_at_restart && differs(p, &running[i], &booted[i])) {
                reply->restart = true;
            }
        }
        if (written) {
            config_version++;   // every accepted write, RAM-only included (§8.1)
            store_set_u32(NVS_KEY_CONFIG_VERSION, config_version);
        }
        reply->result = CONFIG_OK;
        reply->body = config_json_locked(device_id);
    }
    xSemaphoreGive(lock);
    cJSON_Delete(details);

    // Outside the lock: the profile reads the new values back through params_get_*
    for (size_t i = 0; i < count; i++) {
        if (!(changed_now & (1u << i))) {
            continue;
        }
        if (strcmp(table[i]->name, "device_name") == 0) {
            discovery_name_changed();       // re-announce SSDP and re-register mDNS (§8.2)
        } else {
            profile_param_changed(table[i]->name);
        }
    }
    profile_message(PROFILE_MSG_CONFIG, reply->result == CONFIG_OK);
}
