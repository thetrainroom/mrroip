/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * The interface between the MRRoIP core and a device profile (MRROIP-1.md §13.1, §14).
 * The core never names a profile parameter or object; the profile never parses JSON or touches a socket.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "mrroip_emit.h"
#include "mrroip_params.h"
#include "mrroip_value.h"

typedef struct {
    const char *device_type;
    const char *device_class;       // "mobile", "stationary" or "passive" (§4.2)
    const char *profile_version;
    const char *const *modes;       // profile modes, NULL-terminated; never the core ones (§9.3)
    const char *const *target_modes;// modes that require a target, NULL-terminated, or NULL for none
    const char *rest_mode;          // the mode reported at start and after reset (§14 item 5)
    bool autonomous;
    int telemetry_hz;
} profile_info_t;

const profile_info_t *profile_info(void);
const param_desc_t *profile_params(size_t *count);

// Start the device; parameters are loaded by then
void profile_start(void);
// A profile parameter that applies immediately has a new running value
void profile_param_changed(const char *name);
// Entries of the /definition "objects" array
void profile_emit_objects(emit_t *objects);
// Members of state.profile; also reports busy and the fault (NULL if none)
void profile_emit_state(emit_t *profile, bool *busy, const char **fault);
// The current fault, or NULL
const char *profile_fault(void);
// The part of the /definition ETag that depends on how this start is configured
const char *profile_etag(void);

// Control (§9). The core has parsed the message and handles seq, authority and the core modes.
// Check one desired object state without applying it (value.h reads it). Returns NULL if acceptable,
// otherwise a reason for details[].
const char *profile_object_check(const char *id, const object_value_t *value);
// Apply a profile mode and object states already checked. Idempotent: the same desired state changes nothing.
void profile_apply(const char *mode, size_t count, const char *const ids[], const object_value_t *const values[]);
// estop: halt immediately (the core latches it). reset: back to the rest state.
void profile_estop(void);
void profile_reset(void);
// Loss of the master for mobile and stationary devices without a programme (§11.3); passive devices hold
void profile_come_to_rest(void);

// Binary object states, uploaded with PUT /objects/<id> (plan question 16): pixel data too large for a JSON
// message. The core applies seq, authority and the estop and fault latches exactly as for /control, then hands
// the profile the body as it arrives. A profile without such objects need not define these functions: the core's
// weak defaults refuse every upload.
//
// request: the query parameters (integers where they parse as integers) and "base" from the X-MRROIP-Base
// header when it was sent; length: the body's size. Returns NULL if the upload may start, otherwise a reason.
const char *profile_object_stream_begin(const char *id, const object_value_t *request, size_t length);
// A piece of the body; returning false stops the upload, which then ends incomplete
bool profile_object_stream_data(const uint8_t *data, size_t len);
// The body is over; complete is false if the connection broke or fewer bytes arrived than announced
void profile_object_stream_end(bool complete);

typedef enum { PROFILE_MSG_CONTROL, PROFILE_MSG_CONFIG } profile_msg_t;
// Every /control message and /config write after the core handled it, from whichever task received it.
// For local indication only (e.g. an activity LED); it must not change state.
void profile_message(profile_msg_t kind, bool accepted);
