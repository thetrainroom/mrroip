# Conformance vectors

`vectors/*.json` are request → response cases that the unit tests of **every** core in this repository replay
against its own implementation, with the `reference` profile (`PROFILE-REFERENCE.md`), a fake clock and no
network. They pin down the parts of MRROIP-1.md that the probe checks only on a live device —
validation, error shapes, replay, authority — so that the C, Python and Rust cores cannot drift apart
between probe runs.

The probe (`../probe/mrroip_probe.py`) remains the conformance suite. The vectors are cheaper, run in CI,
and never need hardware; they do not replace it.

## Format

A file is `{"cases": [ … ]}`. Each case starts from a **fresh endpoint** (defaults, empty store, clock at
0 ms, no master) and runs its `steps` in order.

```json
{ "name": "stale seq is refused within 5 s",
  "class": "passive",
  "steps": [
    { "control": { "seq": 10, "mode": "on" }, "expect": { "status": 200 } },
    { "control": { "seq": 10, "mode": "on" },
      "expect": { "status": 409, "body": { "accepted": false, "error": "stale_seq" } } },
    { "advance_ms": 5001 },
    { "control": { "seq": 5, "mode": "on" }, "expect": { "status": 200 } }
  ] }
```

| Key | Meaning |
| --- | --- |
| `class` | `device_class` the endpoint is started with; default `passive` |
| `peer` | On a step: the sender's IPv4 address. Default `10.0.0.1` |
| `get` | `GET` this path |
| `config` | `POST /config` with this body (an object, or a string sent verbatim) |
| `control` | `POST /control` with this body (an object, or a string sent verbatim) |
| `udp` | The same body as a UDP control datagram; the reply is compared with `status` absent |
| `advance_ms` | Move the fake clock forward, running whatever the core does on time (timeout, programme) |
| `expect.status` | HTTP status |
| `expect.body` | A **subset** of the response body, compared as below |

### Matching

- An object matches if every key it names is present in the actual object and matches; other keys are
  ignored.
- A list matches only a list of the same length whose elements match in order — unless it is written
  `{"$contains": [ … ]}`, which matches a list containing an element matching each entry.
- `"$any"` matches any value, including `null`; `"$absent"` matches only a missing key.
- Numbers compare by value (`5` equals `5.0`); everything else by equality.

## Grammar cases

`grammar.json` holds declaration → value → reason cases for the check every implementation runs against a
declaration (MRROIP-1.md §7.2, including `one_of`): `mrroip.decl.check` in Python, `Decl::check` in Rust. Each case
is `{"name", "decl", "value", "reason"}`, with `reason` null where the value is accepted. Both test suites replay
the file, so the GUI and the Python endpoint refuse the same values for the same reasons.
