/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * Reading a desired object state without giving the profile a JSON library — the counterpart of emit.h
 * (MRROIP-1.md §13.1). The core passes the parsed value; the profile asks for what it expects and gets
 * NULL, -1 or false for anything else.
 */
#pragma once

#include <stdbool.h>

typedef struct cJSON object_value_t;   // opaque to the profile

// The string, or NULL if `v` is not a string
const char *value_str(const object_value_t *v);
// A member of an object, or NULL if `v` is not an object or has no such member
const object_value_t *value_member(const object_value_t *v, const char *key);
// The number of elements, or -1 if `v` is not an array
int value_array_size(const object_value_t *v);
// An element of an array, or NULL
const object_value_t *value_array_at(const object_value_t *v, int index);
// false unless `v` is a number without a fraction
bool value_int(const object_value_t *v, long *out);
