# MRRoIP — Core Endpoint Specification

**Revision 1.0 · 2026-09-13 · Status: implementation brief**

This document specifies an MRRoIP endpoint **without reference to any particular kind of
device**. Everything here is true of a cable car, a turntable, a signal, a turnout
decoder, a level crossing or a locomotive. Nothing here knows what any of those are.

What a device actually *is* — the things it contains, the states it can be asked for, the
parameters it exposes — is supplied by a **device profile**. §14 lists exactly what a
profile must define, and `MRROIP-PROFILE-CABLECAR.md` is the first one.

The split is not tidiness. A protocol that has a cable car in it will grow a turntable in
it next, and by the fifth device the "protocol" is a pile of special cases nobody can
implement from scratch. The test of this document is that a second profile can be written
against it without changing a line here.

---

## 0. Naming — read this first

**Settled 2026-09-28: the name is `MRRoIP`, "Model RailRoad over IP"** — two Rs because
*RailRoad* is one word with two, which is also how the hobby abbreviates itself (MRR). It
had drifted to `MMRoIP` in the firmware and the library; that spelling is gone from both
and is not accepted anywhere, on the wire or in a filename. Earlier candidates, for the
record: `MRoIP` (one R) and `MRRoverIp`. Because the name appears in a service type, a
header, a JSON field, a filename and an mDNS instance, it stays changeable in one place.

```c
/* proto_name.h — the ONLY place the protocol name is spelled. */
#define MRROIP_NAME        "MRRoIP"          /* human-readable          */
#define MRROIP_TOKEN       "mrroip"          /* lowercase, identifiers  */
#define MRROIP_VERSION     "0.1"             /* draft revision spoken   */
#define MRROIP_UDP_PORT    5300
#define MRROIP_SSDP_ST     "urn:schemas-mrroip-org:device:Endpoint:1"
#define MRROIP_MDNS_SVC    "_mrroip"         /* + "._tcp"               */
```

Never write the literal string anywhere else. A rename must be a one-line diff.

---

## 1. Terminology

| Term | Meaning |
|---|---|
| **Endpoint** | One addressable device on the layout. One MAC, one identity, one `/definition` |
| **Master** | Anything that sends control messages: layout software, a throttle, a script |
| **Profile** | The device-type-specific half of the specification (§14) |
| **Object** | A named, individually addressable thing inside an endpoint — a lamp, an axis, a cabin, a signal head |
| **Authority** | Which master, if any, is currently commanding the endpoint (§11) |
| **Desired state** | What a control message carries. Never a transition (§9.1) |

An endpoint with several objects is still **one** endpoint. A decoder driving eight
turnouts is not eight endpoints; it is one endpoint with eight objects. Identity is per
device because identity is per MAC, and the distinction matters as soon as anything is
configured.

---

## 2. Scope and conformance

A **conformant endpoint** implements §§3–13 in full, plus one profile.
A **conformant master** may use any subset, but must not assume anything a profile has not
declared in `/definition`.

### 2.1 In scope here

Identity · network bring-up · discovery · `/definition` · `/config` · `/control` on HTTP
and UDP · `/state` · control authority and its timeout · the error model · persistence.

### 2.2 Not in scope here, by design

Authentication, TLS, time synchronisation, firmware update, multicast group commands,
IPv6. §16 records why each is absent and what adding it would cost, so that the next
reader can see they were considered rather than forgotten.

### 2.3 Not in scope here, because it belongs in a profile

Everything about motion, position, speed, outputs, sensors, faults and device-specific
parameters. If a rule cannot be stated without naming a kind of device, it is a profile
rule and belongs in the profile document.

---

## 3. Device classes

Every endpoint declares exactly one `device_class`. It is not decoration: it is the hook
the control timeout hangs on (§11.3), and it is the only place the protocol needs to know
anything about the nature of the device.

| `device_class` | Meaning | On loss of the master |
|---|---|---|
| `mobile` | Moves along the layout under a master's direction. A locomotive | **Stops.** Nothing else is safe |
| `stationary` | Fixed in place. May move a load on its own schedule | **Returns to autonomous operation**, or comes to rest if it has none |
| `passive` | Does not move anything with momentum. A signal, a light, a turnout | Holds its last state |

A profile fixes its class; an endpoint does not choose at runtime. If a future device
genuinely fits none of these, the right response is a fourth class in this document, not a
special case in that profile.

---

## 4. Identity

| Field | Rule |
|---|---|
| `device_id` | Wi-Fi **station** MAC, lowercase hex, colon-separated: `a0:b7:65:12:34:56`. On ESP-IDF, `esp_read_mac(mac, ESP_MAC_WIFI_STA)`. Never the AP MAC, which differs. Immutable, and **not settable through `/config`** |
| `device_name` | Free text, ≤31 characters. A **label, never an identifier**. Two endpoints may legitimately share one until somebody renames one of them |
| `device_type` | The profile's identifier, e.g. `"turntable"`, `"cablecar"`. Lowercase, no spaces |
| `device_class` | §3 |
| `profile_version` | The profile revision the firmware implements |
| `firmware` | Semantic version from a build-time define |

A client that renames a device must still find it by MAC afterwards. If renaming changes
identity, the whole discovery model collapses — every master's stored references break the
first time a user types a friendlier name.

---

## 5. Network and transport

### 5.1 Bring-up

```
boot
 └─> credentials stored?
      ├─ yes -> station mode, 20 s timeout, 3 attempts
      │          ├─ ok   -> services up
      │          └─ fail -> fallback AP
      └─ no  -> fallback AP
```

**Fallback AP:** SSID `<device_type>-XXXXXX` (last 3 MAC octets), WPA2, IP `192.168.4.1`.

**All protocol services run identically in AP mode.** Do not gate the HTTP server or the
UDP listener on station mode. An endpoint that only speaks the protocol once it is on
somebody's network cannot be tested on a bench, and cannot be diagnosed when the network
is the thing that is broken.

### 5.2 Ports

| Service | Port | Note |
|---|---|---|
| HTTP | 80 | All request/response endpoints |
| Control | UDP 5300 | Configurable. Bound to `0.0.0.0`; accepts broadcast |
| SSDP | UDP 1900 | Multicast `239.255.255.250` |
| whois probe | UDP 8266 | Diagnostic (§6.3) |
| mDNS | 5353 | Convenience (§6.2) |

### 5.3 Encoding

JSON, UTF-8, no BOM, `Content-Type: application/json` on every response. Maximum accepted
request body 4096 bytes; larger returns `413`. Every endpoint must work with both
`Connection: close` and keep-alive.

---

## 6. Discovery

### 6.1 SSDP — normative

Announce `ssdp:alive` at start-up **three times, 100 ms apart** (UDP multicast is lossy and
the start-up announcement is the one that matters), then every `announce_interval_s`
(default 300, minimum 30). Respond to `M-SEARCH` for `ssdp:all` or for the MRRoIP search
target, after a random delay of 0…`MX` seconds as RFC 1123 requires. Send `ssdp:byebye`
on a clean reboot.

```
NOTIFY * HTTP/1.1
HOST: 239.255.255.250:1900
CACHE-CONTROL: max-age=600
LOCATION: http://192.168.1.47/definition
NT: urn:schemas-mrroip-org:device:Endpoint:1
NTS: ssdp:alive
USN: uuid:mrroip-a0b765123456::urn:schemas-mrroip-org:device:Endpoint:1
SERVER: esp-idf/5.2 MRRoIP/0.1
X-MRROIP-ID: a0:b7:65:12:34:56
X-MRROIP-NAME: drehscheibe
X-MRROIP-TYPE: turntable
X-MRROIP-CLASS: stationary
```

`LOCATION` points at `/definition` directly, not at a UPnP device description. MRRoIP does
not use UPnP's XML document, and implying otherwise would oblige every implementer to
serve one.

The four `X-MRROIP-*` headers let a master build a device list without fetching anything.
On a layout with sixty endpoints, that is the difference between a discovery pass costing
one multicast round trip and costing sixty HTTP requests.

**Rate limiting and recovery.** A master may silence announcements with
`POST /config {"announce_interval_s": 0}`, as the standard requires. If announcements are
disabled **and** no `/control` or `/config` traffic has been seen for 30 minutes, re-enable
them at the default interval. A layout whose master has gone away must become discoverable
again without a power cycle; otherwise the first thing a user learns about this standard is
that it can lose a device permanently.

### 6.2 mDNS — convenience, not protocol

Advertise `_mrroip._tcp` on port 80 with TXT records `id`, `name`, `type`, `class`, `fw`,
and set the hostname so `<device_name>.local` resolves. This exists so a person can type a
name into a browser. **Nothing in the protocol may depend on it** — a conformance run must
pass with mDNS compiled out.

### 6.3 whois probe — diagnostic

Listen on UDP 8266. On a datagram whose payload contains `"whois"`, reply to the sender:

```json
{"proto":"MRRoIP","v":"0.1","id":"a0:b7:65:12:34:56","name":"drehscheibe",
 "type":"turntable","class":"stationary","ip":"192.168.1.47","definition":"/definition"}
```

Twenty lines of code. It exists because consumer mesh routers and guest networks silently
block client-to-client multicast, and when SSDP finds nothing there is otherwise no way to
distinguish a missing device from a blocked one — which is the worst class of failure to
put in front of a non-technical owner. Diagnostic aid, not a second discovery mechanism.

---

## 7. `GET /definition`

The endpoint's self-description: what it is, what it contains, what it accepts, and every
configurable parameter with type, default, range and unit.

**This document is generated from the same table that validates `/config` writes.** One
`static const param_desc_t params[]`, walked once to emit JSON and once to validate. Two
hand-maintained lists diverge within a week, and a master that trusts a stale
`/definition` is worse than one with none.

Cacheable. Send an `ETag` derived from firmware version plus profile version, and serve
`304`.

The examples throughout this document describe a **turntable**, deliberately — the first
profile written against this specification is a cable car, and a core document illustrated
with its own first profile is how a protocol quietly acquires that profile's assumptions.

```json
{
  "proto": "MRRoIP",
  "proto_version": "0.1",

  "device_id": "a0:b7:65:12:34:56",
  "device_name": "drehscheibe",
  "device_type": "turntable",
  "device_class": "stationary",
  "profile_version": "1.0",
  "firmware": "0.1.0",

  "endpoints": {
    "definition": "/definition",
    "config": "/config",
    "control": "/control",
    "state": "/state",
    "udp_control_port": 5300
  },

  "capabilities": {
    "autonomous": true,
    "commanded": true,
    "telemetry_hz": 5,
    "modes": ["goto", "index", "stop"],
    "core_modes": ["estop", "reset", "release", "hold"]
  },

  "objects": [
    { "id": "bridge", "kind": "axis",   "profile": { "tracks": 24 } },
    { "id": "lamp",   "kind": "output", "states": ["off", "on"] }
  ],

  "parameters": [
    { "name": "device_name", "type": "string", "default": "drehscheibe",
      "max_len": 31, "persist": true, "doc": "Human label. Not an identifier." },
    { "name": "control_timeout_ms", "type": "int", "default": 2000,
      "min": 500, "max": 30000, "unit": "ms", "persist": true },
    { "name": "udp_port", "type": "int", "default": 5300,
      "min": 1024, "max": 65535, "persist": true },
    { "name": "announce_interval_s", "type": "int", "default": 300,
      "min": 0, "max": 86400, "unit": "s", "persist": true,
      "doc": "0 disables SSDP announcements; see rate-limit recovery." }
  ]
}
```

### 7.1 Core versus profile content

| Field | Owner |
|---|---|
| `proto`, `proto_version`, `endpoints` | Core, fixed |
| `device_id`, `device_name`, `firmware` | Core |
| `device_type`, `device_class`, `profile_version` | Profile declares, core carries |
| `capabilities.core_modes` | Core. Always exactly `estop`, `reset`, `release`, `hold` |
| `capabilities.modes` | Profile |
| `capabilities.autonomous`, `.commanded`, `.telemetry_hz` | Profile |
| `objects[].id`, `.kind`, `.states` | Profile |
| `objects[].profile` | Profile, opaque to core. Anything the profile needs; a master that does not know the type ignores it |
| `parameters[]` | Four core parameters above are mandatory; the profile appends its own |

A master that understands the core but not the profile must still be able to discover the
device, read its config, see its state, and issue `estop`. That is the minimum useful
behaviour and it must not require profile knowledge — the emergency stop on a layout
cannot be gated on whether the software has been updated for a device it has never seen.

---

## 8. Configuration

### 8.1 `GET /config`

Current **effective** values — RAM, which may differ from flash — plus a `_meta` block.

```json
{
  "device_id": "a0:b7:65:12:34:56",
  "config": { "device_name": "drehscheibe", "control_timeout_ms": 2000 },
  "_meta": { "dirty": true, "dirty_keys": ["control_timeout_ms"], "config_version": 7 }
}
```

Without `dirty`, a client cannot tell an experiment from a saved setting, and the
semantics below become invisible from outside. `config_version` increments on every
accepted write, RAM-only included, and is the concurrency token.

### 8.2 `POST /config` — commit semantics

**This is the first of the three points where the working draft is silent and an
implementation has to decide.**

```json
{ "control_timeout_ms": 4000, "persist": true, "if_version": 7 }
```

| Rule | Behaviour |
|---|---|
| Application | Applies to the running system **immediately**, in RAM |
| Persistence | Written to flash **only** when the body carries `"persist": true` |
| Power cycle | Discards anything not persisted. The forgiving default: somebody sliding a value around gets back to a known state by pulling the plug |
| Atomicity | **All keys or none.** Validate everything first; on any failure apply nothing and list **every** offending key, not just the first |
| Unknown keys | Rejected, `400`. Silently ignoring them makes a typo look like success, which is how somebody spends an evening wondering why a setting does nothing |
| Concurrency | `if_version` optional. Present and ≠ `config_version` → `409` with the current config. Two masters writing at once is rare; without this the failure is silent |
| Reserved | `persist`, `if_version` and `factory_reset` are control keys, never parameters |
| Response | `200`, body identical to `GET /config` |
| Side effects | A change that alters transport (e.g. `udp_port`) takes effect **after** the response is sent. A change to `device_name` re-announces SSDP and re-registers mDNS |

```json
{ "error": "validation_failed",
  "applied": false,
  "details": [
    { "key": "control_timeout_ms", "reason": "out_of_range", "min": 500, "max": 30000 },
    { "key": "contrl_timeout",     "reason": "unknown_key" }
  ] }
```

`"applied": false` is stated explicitly rather than implied by the status code. A partial
application the client does not know about is the worst outcome available here.

**Factory reset:** `POST /config {"factory_reset": true}` erases the namespace and reboots.

---

## 9. Control

### 9.1 Desired state, not transitions

**The second decision.** The draft requires control messages to be repeated at ≥1 Hz on the
UDP path. A message meaning "start" cannot survive that: sent ten times it must not start
ten things.

So every control message carries the **desired state**. `{"mode":"run"}` means *"I want you
running"*, not *"begin now"*. Re-sending it while already running changes nothing. Recovery
from packet loss is then free — the next repeat re-establishes the correct state without
the master tracking what was missed.

This is also why `/control` and `/state` use the same vocabulary: the master says what it
wants in exactly the terms the endpoint reports back.

### 9.2 Message

`POST /control`, and the byte-identical JSON as a UDP datagram to the control port:

```json
{ "seq": 1043, "ts": 88123, "mode": "goto", "target": "track7",
  "objects": { "lamp": "on" } }
```

| Field | Req | Meaning |
|---|---|---|
| `seq` | yes | Monotonic per master. Echoed. Detects loss; **never** used to sequence actions |
| `ts` | no | Master's monotonic ms. Echoed. One-way delay estimation only; clocks are not synchronised |
| `mode` | yes | A core mode (§9.3) or a profile mode declared in `capabilities.modes` |
| `target` | profile | An object id, when the profile's mode requires one |
| `objects` | no | Desired state of named objects. Orthogonal to `mode` |
| `hold` | no | `true` refreshes authority without changing anything (§11.2) |

**Objects are set by desired state, never by a mode.** A mode enum that mixes motion with
outputs stops composing the moment a second output exists, and every profile has a second
output eventually.

### 9.3 Core modes

Every endpoint implements these four, whatever its profile:

| `mode` | Meaning |
|---|---|
| `estop` | Halt everything **immediately**. Latches; requires `reset`. Honoured from the UDP path with no handshake, priority over anything in flight |
| `reset` | Clear a latched `estop` or fault. Returns to the profile's rest state |
| `release` | The master relinquishes authority (§11) |
| `hold` | No change. Refreshes authority |

Profile modes are declared in `capabilities.modes` and defined by the profile document.

### 9.4 Response

Identical body for HTTP and UDP; on UDP it goes back to the sender's address and port.

```json
{
  "device_id": "a0:b7:65:12:34:56",
  "ack_seq": 1043,
  "ts": 88123,
  "accepted": true,
  "state": {
    "mode": "goto",
    "authority": "commanded",
    "busy": true,
    "fault": null,
    "uptime_ms": 903412,
    "profile": { "phase": "slewing", "angle_deg": 172.4, "lamp": "on" }
  }
}
```

| State field | Owner |
|---|---|
| `mode`, `authority`, `fault`, `uptime_ms` | Core |
| `busy` | Core. True when the endpoint is doing something a master should wait for. The profile decides what counts |
| `profile` | Profile. Opaque to core; a master that does not know the type ignores it |
| Object states | Reported at the top level of `profile`, keyed by object id |

A rejection:

```json
{ "device_id": "...", "ack_seq": 1043, "accepted": false,
  "error": "latched_estop", "state": { "...": "..." } }
```

### 9.5 Sequence numbers and replay

Track `last_seq` per master address. A datagram whose `seq` is ≤ `last_seq` and which
arrives within 5 s of the last accepted one is a **reordered duplicate**: ignore it, reply
`accepted:false, error:"stale_seq"`. A gap of more than 5 s means the master restarted its
counter — accept and re-anchor. Without that carve-out a master reboot locks itself out
until the endpoint is power-cycled, which is a cruel way to spend an evening.

### 9.6 `GET /state`

The `state` object of §9.4 alone. No side effects, no authority acquisition.

**Polling `/state` must never count as a control message.** Monitoring and commanding are
different acts; conflating them makes the timeout rule unanalysable, because a master
that is merely watching would silently keep authority alive.

---

## 10. Errors

| HTTP | Body `error` | When |
|---|---|---|
| 400 | `malformed_json` | Unparseable |
| 400 | `validation_failed` | `/config`, with `details[]` |
| 400 | `unknown_mode` | Not a core mode and not in `capabilities.modes` |
| 400 | `missing_target` / `unknown_target` | Profile mode needs an object id |
| 404 | `not_found` | Unknown path |
| 405 | `method_not_allowed` | Wrong verb |
| 409 | `version_conflict` | Stale `if_version`; body carries current config |
| 409 | `latched_estop` / `latched_fault` | Command refused while latched |
| 413 | `body_too_large` | > 4096 bytes |
| 503 | `not_ready` | Still bringing up |

On UDP there is no status code, so `accepted: false` plus `error` carries it. The strings
are identical on both transports; a master must not need two error tables.

---

## 11. Authority and the control timeout

### 11.1 States

| `authority` | Meaning |
|---|---|
| `autonomous` | No master. The endpoint runs its own programme, if its profile has one |
| `commanded` | A master has sent control within `control_timeout_ms` |
| `idle` | No master and no autonomous programme. At rest |

### 11.2 Acquiring and holding

Any accepted control message sets `authority = commanded` and records the master's address
and the time. A master holds authority by repeating its message at ≥1 Hz — which it is
doing anyway, because the message is a desired state. `{"mode":"hold"}` refreshes without
changing anything.

One master at a time. A control message from a different address while authority is held
**is accepted** and authority transfers, but the response carries
`"authority_taken_from": "<previous address>"` so a client can notice. Locking would need
an arbitration scheme the draft does not have, and a layout running two throttles by
accident should be diagnosable rather than mysteriously dead.

### 11.3 The timeout — the third decision

The draft says a **moving object** must stop if no control message arrives within 2 s.
Applied literally to a device that moves on its own schedule, an owner who never connects
it to anything gets something that runs for two seconds and then stops forever. The rule
is written for a locomotive under a throttle, where nothing else is a sensible failure
mode.

**The rule this specification adopts:**

```
The control timeout is armed only while authority == "commanded".
On expiry, or on an explicit "release", dispatch on device_class:

    mobile      -> stop. Authority becomes idle.
    stationary  -> if the profile has an autonomous programme: resume it,
                   authority becomes autonomous;
                   otherwise come to rest as the profile defines, authority idle.
    passive     -> hold the last commanded state, authority idle.

Autonomous motion is NEVER subject to the timeout.
A latched estop survives every transition. Loss of a master must not clear a safety stop.
```

The distinction the draft needs is **commanded motion** versus **motion**, and
`device_class` is where it belongs. Log every transition —
`AUTHORITY commanded -> autonomous (timeout, master 192.168.1.5)` — because this is the
rule most likely to be argued about, and a behaviour that cannot be observed cannot be
argued about.

---

## 12. Persistence

One NVS namespace, `mrroip`. One key per persisted parameter, named identically to the
parameter, so the mapping needs no table. Plus:

| Key | Purpose |
|---|---|
| `cfg_ver` | `config_version`, survives reboot |
| `wifi_ssid`, `wifi_pass` | Credentials |

Never write flash from a timer callback or directly from an HTTP handler thread; queue it.
Rate-limit any counter a profile persists — flash wear is measured in write cycles, not in
years.

---

## 13. Implementation constraints

These are not style preferences. Each one is a failure that has to be designed out rather
than tested out.

1. **One control parser.** HTTP and UDP both call a single
   `control_apply(const char *json, size_t len, char *out, size_t outlen)`. Two parsers
   means two behaviours, and the divergence will be found by a user rather than by a test.
2. **One parameter table.** `/definition` generation and `/config` validation walk the same
   array (§7).
3. **One protocol name.** §0.
4. **The profile is a module.** Core code must not include a profile header, and profile
   code must not parse JSON or touch a socket. The interface between them is a small C
   header, and if anything in core knows a profile's parameter names, the split has
   already failed.
5. **Services run in AP mode.** §5.1.

---

## 14. What a device profile must define

A profile document is not conformant until it answers every one of these. This is the
checklist a second profile author works from, and the reason this document does not
mention cable cars.

| # | The profile must define | Core's stake in it |
|---|---|---|
| 1 | `device_type` string and `profile_version` | Identity and `/definition` |
| 2 | `device_class` (§3) | Fixes the timeout behaviour of §11.3 |
| 3 | The objects: id, `kind`, and for outputs the legal `states` | `/definition.objects`, and `objects` in control |
| 4 | The modes beyond the four core ones, each as a **desired state** | `capabilities.modes`, validation of `mode` |
| 5 | Which modes require a `target`, and what a legal target is | `missing_target` / `unknown_target` |
| 6 | The parameters: name, type, default, range, unit, whether persisted | Appended to the core four |
| 7 | The contents of `state.profile`, including a `phase` enum | Reported verbatim |
| 8 | What `busy` means for this device | Core reports it; only the profile knows |
| 9 | The rest state — what `reset` returns to and what "come to rest" means on timeout | §11.3 |
| 10 | The autonomous programme, or an explicit statement that there is none | §11.3 dispatch |
| 11 | The fault set: every value `state.fault` can take, how each is detected, how each is cleared | `latched_fault` |
| 12 | What `estop` does physically, and what state it leaves behind | §9.3 |
| 13 | Its own conformance tests, in the form of §15 | Run after the core suite |
| 14 | **Core-suite hooks** (§15.1) | Lets the generic tests make the device actually do something |

A profile may **not** redefine anything in §§3–13, add an endpoint path, add a transport,
or change the meaning of a core mode. If it needs to, the core document is wrong and
should be changed — for everyone, not locally.

---

## 15. Core conformance tests

Run by `mrroip_probe.py` against **any** endpoint, whatever its profile, using discovery
alone. Profile tests run afterwards, from the profile's own module.

| # | Test | Pass condition |
|---|---|---|
| C-1 | SSDP discovery | Found within 5 s of an `M-SEARCH`; `LOCATION` fetches |
| C-2 | SSDP headers | All four `X-MRROIP-*` present and matching `/definition` |
| C-3 | mDNS | `<device_name>.local` resolves to the same address |
| C-4 | whois probe | Same `device_id` as SSDP and `/definition` |
| C-5 | Identity | `device_id` is the station MAC; unchanged after a rename |
| C-6 | `/definition` shape | All required keys; every numeric parameter has a range, every string a `max_len` |
| C-7 | Class declared | `device_class` is one of the three, `device_type` and `profile_version` present |
| C-8 | Core modes declared | `core_modes` is exactly the four of §9.3 |
| C-9 | Definition ↔ config | Key sets identical |
| C-10 | Config applies | Write → `GET /config` shows it; `_meta.dirty` and `dirty_keys` correct |
| C-11 | ★ Config volatility | Without `persist` → reboot → old value. With → reboot → new value |
| C-12 | Atomicity | One valid + one invalid key → `400`, both listed, **neither applied** |
| C-13 | Unknown key | `400`, `unknown_key`, nothing applied |
| C-14 | Range check | Below `min` and above `max` rejected; the bound itself accepted |
| C-15 | Concurrency | Stale `if_version` → `409` carrying the current config |
| C-16 | ★ Idempotence | The same control message ×10 at 1 Hz has the effect of one |
| C-17 | Seq echo | `ack_seq` matches, on both transports |
| C-18 | Replay | Duplicate `seq` → `stale_seq`; after a 5 s gap a lower `seq` is accepted |
| C-19 | Transport parity | Identical JSON over HTTP and UDP → identical `state` |
| C-20 | Unknown mode | A mode in neither list → `400 unknown_mode` |
| C-21 | ★ Timeout by class | `mobile` stops; `stationary` with a programme keeps going as `autonomous`; `passive` holds |
| C-22 | ★ Unattended | With no control message ever sent, the endpoint does what its class says, indefinitely |
| C-23 | Release | `release` → authority drops **immediately**, without waiting out the timeout |
| C-24 | estop | Stops within 100 ms; latches; survives a control timeout; `reset` clears it |
| C-25 | `/state` passive | Polling at 5 Hz does not keep authority alive |
| C-26 | Authority transfer | A second address is accepted and `authority_taken_from` appears |
| C-27 | Error strings | Same `error` values on HTTP and UDP for the same fault |
| C-28 | Rate-limit recovery | `announce_interval_s = 0`, no traffic → announcements resume |
| C-29 | Malformed input | Truncated JSON, oversized body, binary garbage → correct error, no crash, still responsive |
| C-30 | AP-mode parity | With no credentials, on the fallback AP, C-1/C-6/C-10/C-16 pass unchanged |
| C-31 | Soak | 30 min unattended, `/state` polled at 5 Hz → no reboot, no heap decline over the last 20 min |

### 15.1 Core-suite hooks

Four core tests — C-16, C-21, C-22, C-24 — are generic in principle but need the device to
*do* something, and only the profile knows how to ask. The profile supplies:

| Hook | Meaning |
|---|---|
| `activate` | A mode that makes the endpoint busy for a while |
| `rest` | A mode that returns it to rest. Defaults to `hold` |
| `counter` | A field in `state.profile` counting completed activities |
| `cycle_seconds(d)` | Roughly how long one activity takes, for sizing waits |
| `prepare_fast(d)` | Configure short cycles so tests take seconds, not minutes |
| `provoke_fault(d)` | Put the device into `state.fault`, or return false |

Without a profile module these four **skip**, with a note saying so. They do not guess a
mode name: a core suite that invents `"run"` is testing the profile it imagined rather than
the endpoint in front of it, and a green run would mean nothing.

★ marks the four that exist to expose the three decisions of §§8.2, 9.1 and 11.3. If those
four pass, the draft's ambiguities have answers that survive contact with a real device. If
they fail, the implementation has chosen wrongly or not at all, and the summary says which.

C-31 matters more than it looks: a leak in a JSON path is the classic ESP32 failure and it
does not show in a five-minute run.

---

## 16. Deliberate omissions

| Omitted | Why | Cost of adding |
|---|---|---|
| Authentication | The draft has none. Anything on the layout LAN is controllable by anything else on it | A bearer token in a header; structurally free |
| TLS | Certificate management on a €40 device with no clock is not realistic | High, and it buys little on a private LAN |
| Time sync | `ts` is each party's own monotonic clock, explicitly unsynchronised | SNTP is cheap, but only helps logging |
| Multicast control | A group `estop` would be genuinely useful and is the strongest candidate for the next revision | A reserved group, identical payload |
| Firmware update | Well-trodden in ESP-IDF, nothing protocol-shaped about it | — |
| IPv6 | The draft is silent. Note that the ~250-device ceiling attributed to IPv4 in comparable systems is a DHCP-pool and AP-association limit, not an address-space one | Moderate |

---

## 17. Questions this specification puts back to the working draft

Each is answered above by a decision that a running implementation can be measured
against. None of them can be settled on paper.

1. **Does the 2 s timeout apply to an endpoint that moves on its own?** Answered by
   `device_class` dispatch (§11.3). The draft should adopt the distinction between
   commanded motion and motion.
2. **Does a configuration write apply, persist, or need a commit?** Answered by
   `persist: true` with atomic validation (§8.2). The draft says nothing, and every
   implementer will guess differently.
3. **Is a control message a setpoint or an event?** Answered by desired-state semantics
   (§9.1). The draft's ≥1 Hz repeat requirement makes this mandatory, but does not say so.
4. **Are endpoint classes part of the standard?** They should be. Without them every
   device re-argues the timeout question, which is exactly what happened here.
5. **Where does device-specific behaviour live?** In a profile, structured as §14. A
   standard whose core document names particular devices cannot be implemented from
   scratch by its fifth device.

---

*Companion documents: `MRROIP-PROFILE-CABLECAR.md` (the first profile, and the ESP32 test
build), the Brawa cable car ESP32 controller specification (§6.9 records the same three
decisions as they apply to the real board), and the MRRoIP working-draft review
(findings 3–7).*
