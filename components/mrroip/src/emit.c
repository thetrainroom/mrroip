/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
#include "cJSON.h"
#include "mrroip_emit.h"

static emit_t *add(emit_t *parent, const char *key, cJSON *item)
{
    if (!parent || !item) {
        cJSON_Delete(item);
        return NULL;
    }
    if (cJSON_IsArray(parent)) {
        cJSON_AddItemToArray(parent, item);
    } else {
        cJSON_AddItemToObject(parent, key, item);
    }
    return item;
}

void emit_int(emit_t *parent, const char *key, long long value)
{
    add(parent, key, cJSON_CreateNumber((double)value));
}

void emit_str(emit_t *parent, const char *key, const char *value)
{
    add(parent, key, cJSON_CreateString(value));
}

void emit_bool(emit_t *parent, const char *key, bool value)
{
    add(parent, key, cJSON_CreateBool(value));
}

void emit_null(emit_t *parent, const char *key)
{
    add(parent, key, cJSON_CreateNull());
}

emit_t *emit_object(emit_t *parent, const char *key)
{
    return add(parent, key, cJSON_CreateObject());
}

emit_t *emit_array(emit_t *parent, const char *key)
{
    return add(parent, key, cJSON_CreateArray());
}
