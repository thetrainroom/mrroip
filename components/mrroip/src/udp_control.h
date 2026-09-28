/* SPDX-FileCopyrightText: 2026 Thierry Gschwind
 * SPDX-License-Identifier: Apache-2.0
 */
/*
 * UDP transport for control (MRROIP-1.md §5.3, §9.2): the byte-identical JSON as a datagram to
 * udp_port, bound to 0.0.0.0; the response goes back to the sender's address and port.
 */
#pragma once

void udp_control_start(void);
