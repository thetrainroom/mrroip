#include <math.h>
#include "cJSON.h"
#include "mmroip_value.h"

const char *value_str(const object_value_t *v)
{
    return cJSON_IsString(v) ? v->valuestring : NULL;
}

const object_value_t *value_member(const object_value_t *v, const char *key)
{
    return cJSON_IsObject(v) ? cJSON_GetObjectItemCaseSensitive(v, key) : NULL;
}

int value_array_size(const object_value_t *v)
{
    return cJSON_IsArray(v) ? cJSON_GetArraySize(v) : -1;
}

const object_value_t *value_array_at(const object_value_t *v, int index)
{
    return cJSON_IsArray(v) ? cJSON_GetArrayItem(v, index) : NULL;
}

bool value_int(const object_value_t *v, long *out)
{
    if (!cJSON_IsNumber(v) || v->valuedouble != floor(v->valuedouble) || fabs(v->valuedouble) > 1e9) {
        return false;
    }
    *out = (long)v->valuedouble;
    return true;
}
