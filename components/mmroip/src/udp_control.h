/*
 * UDP transport for control (MMROIP-CORE-SPEC.md §5.2, §9.2): the byte-identical JSON as a datagram to
 * udp_port, bound to 0.0.0.0; the response goes back to the sender's address and port.
 */
#pragma once

void udp_control_start(void);
