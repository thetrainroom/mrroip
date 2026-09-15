/*
 * Control (MMROIP-CORE-SPEC.md §9–§11): the one parser for HTTP and UDP, sequence numbers and replay,
 * authority with its timeout, and the core modes estop / reset / release / hold.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

struct cJSON;

// Start the authority timeout; the profile must be running
void control_start(void);

// The one control parser (§13.1). source_ip is the sender's IPv4 address in network byte order.
// Writes the JSON response to out and returns the HTTP status: 200, 400 or 409.
int control_apply(const char *json, size_t len, uint32_t source_ip, char *out, size_t outlen);

// A rejection that never reached the parser (e.g. body_too_large), in the same shape
void control_reject(const char *error, char *out, size_t outlen);

// The §9.4 state object; the caller deletes it
struct cJSON *control_state_json(void);
