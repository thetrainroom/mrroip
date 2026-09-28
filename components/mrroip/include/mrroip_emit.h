/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * JSON output for the profile module without giving it a JSON library (MRROIP-1.md §13.1: profile
 * code does not parse JSON). The core creates the root and owns everything added to it.
 */
#pragma once

#include <stdbool.h>

typedef struct cJSON emit_t;    // opaque to the profile

// `key` names the member in an object; pass NULL when `parent` is an array
void emit_int(emit_t *parent, const char *key, long long value);
void emit_str(emit_t *parent, const char *key, const char *value);
void emit_bool(emit_t *parent, const char *key, bool value);
void emit_null(emit_t *parent, const char *key);
emit_t *emit_object(emit_t *parent, const char *key);
emit_t *emit_array(emit_t *parent, const char *key);
