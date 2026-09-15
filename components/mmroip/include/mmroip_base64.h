/*
 * RFC 4648 Base64 decoding. mbedTLS is deliberately not linked (MMROIP-PLAN.md §5), so this is local.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

// Decode padded Base64. Returns the number of bytes written, or -1 if the input is not valid
// Base64 or would not fit into out_size bytes.
int base64_decode(const char *in, size_t in_len, uint8_t *out, size_t out_size);
