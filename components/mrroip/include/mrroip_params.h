/*
 * The one parameter table (MRROIP-1.md §7, §7.2): /definition is generated from it and /config
 * writes are validated against it. The four core parameters are defined here; the profile appends its own.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PARAM_STR_MAX   32

typedef enum { PARAM_INT, PARAM_STRING } param_type_t;

typedef struct {
    const char *name;
    const char *nvs_key;            // NULL: same as name. ESP-IDF NVS keys have at most 15 characters
    param_type_t type;
    int32_t default_int;
    const char *default_str;        // NULL for device_name: the profile's device_type
    int32_t min;                    // PARAM_INT, inclusive
    int32_t max;
    uint8_t max_len;                // PARAM_STRING, below PARAM_STR_MAX
    const char *const *values;      // PARAM_STRING: NULL-terminated legal values, or NULL for free text
    const char *unit;
    bool applies_at_restart;        // accepted only with persist; the endpoint restarts to apply it
    const char *doc;
} param_desc_t;

struct cJSON;

// Build the table and load stored values. Call before the profile starts.
void params_init(void);
int32_t params_get_int(const char *name);
void params_get_str(const char *name, char *out, size_t size);

// Entries of /definition "parameters"
void params_emit_definition(struct cJSON *array);
// Body of GET /config (§8.1)
struct cJSON *params_config_json(const char *device_id);

typedef enum { CONFIG_OK, CONFIG_BAD_REQUEST, CONFIG_CONFLICT } config_result_t;

typedef struct {
    config_result_t result;
    struct cJSON *body;             // response body; the caller sends and deletes it
    bool restart;                   // restart once the response is sent
    bool factory_reset;             // erase the namespace, then restart, once the response is sent
} config_reply_t;

// POST /config (§8.2): validate every key first, then apply all of them or none
void params_config_apply(const char *json, size_t len, const char *device_id, config_reply_t *reply);
