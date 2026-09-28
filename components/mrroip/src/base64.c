/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
#include "mrroip_base64.h"

static int sextet(char c)
{
    if (c >= 'A' && c <= 'Z') {
        return c - 'A';
    }
    if (c >= 'a' && c <= 'z') {
        return c - 'a' + 26;
    }
    if (c >= '0' && c <= '9') {
        return c - '0' + 52;
    }
    if (c == '+') {
        return 62;
    }
    if (c == '/') {
        return 63;
    }
    return -1;
}

int base64_decode(const char *in, size_t in_len, uint8_t *out, size_t out_size)
{
    if (in_len % 4 != 0) {
        return -1;
    }
    size_t written = 0;
    for (size_t i = 0; i < in_len; i += 4) {
        uint32_t quantum = 0;
        int padding = 0;
        for (int k = 0; k < 4; k++) {
            char c = in[i + k];
            int value = 0;
            if (c == '=') {
                // Padding only in the last quantum, only in its last two positions, and only at the end
                if (i + 4 != in_len || k < 2) {
                    return -1;
                }
                padding++;
            } else {
                value = sextet(c);
                if (value < 0 || padding > 0) {
                    return -1;
                }
            }
            quantum = (quantum << 6) | (uint32_t)value;
        }
        int bytes = 3 - padding;
        if (written + bytes > out_size) {
            return -1;
        }
        out[written++] = (uint8_t)(quantum >> 16);
        if (bytes > 1) {
            out[written++] = (uint8_t)(quantum >> 8);
        }
        if (bytes > 2) {
            out[written++] = (uint8_t)quantum;
        }
    }
    return (int)written;
}
