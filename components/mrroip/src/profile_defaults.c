/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
#include "mrroip_profile.h"

// A profile without binary objects defines none of these: every upload is refused (plan question 16)

__attribute__((weak)) const char *profile_object_stream_begin(const char *id, const object_value_t *request,
                                                              size_t length)
{
    return "not_uploadable";
}

__attribute__((weak)) bool profile_object_stream_data(const uint8_t *data, size_t len)
{
    return false;
}

__attribute__((weak)) void profile_object_stream_end(bool complete)
{
}
