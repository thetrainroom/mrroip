/*
 * Persistence (MMROIP-CORE-SPEC.md §12): every flash write goes through one task, never directly from an
 * HTTP handler or a timer. Namespace MMROIP_TOKEN.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"

void store_start(void);
void store_set_i32(const char *key, int32_t value);
void store_set_u32(const char *key, uint32_t value);
void store_set_str(const char *key, const char *value);
void store_erase_all(void);

// Wait until everything queued so far has reached flash. Returns false on timeout.
bool store_flush(TickType_t timeout);
