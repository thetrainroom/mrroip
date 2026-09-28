/*
 * Control (MRROIP-1.md §9–§11): the one parser for HTTP and UDP, sequence numbers and replay,
 * authority with its timeout, and the core modes estop / reset / release / hold.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "mrroip_value.h"

struct cJSON;

// Start the authority timeout; the profile must be running
void control_start(void);

// The one control parser (§13.1). source_ip is the sender's IPv4 address in network byte order.
// Writes the JSON response to out and returns the HTTP status: 200, 400 or 409.
int control_apply(const char *json, size_t len, uint32_t source_ip, char *out, size_t outlen);

// Binary object uploads (PUT /objects/<id>): admission follows /control exactly, for a change of one object that
// keeps the current mode. 200 means the caller streams the body to the profile and then calls
// control_stream_end(); anything else is the finished reply. The control lock is not held while the body arrives.
int control_stream_begin(const char *object_id, const object_value_t *request, bool have_seq, double seq,
                         size_t length, uint32_t source_ip, char *out, size_t outlen);
// Ends an upload started with 200 and writes its reply
int control_stream_end(bool complete, bool have_seq, double seq, char *out, size_t outlen);

// The address of the master holding authority now, in network byte order, or 0 if none (§11)
uint32_t control_master_ip(void);

// A rejection that never reached the parser (e.g. body_too_large), in the same shape
void control_reject(const char *error, char *out, size_t outlen);

// The §9.4 state object; the caller deletes it
struct cJSON *control_state_json(void);
