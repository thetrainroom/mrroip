# Profile `reference` 1.0

A lamp that can be on, off or blinking — the smallest device that exercises everything the core suite
needs a device to *do*. Every implementation of the core in this repository ships it, so that
`probe/mrroip_probe.py` runs C-16, C-21, C-22 and C-24 on each of them without display hardware.
Nothing here is specific to one implementation; where this document and an implementation disagree, one
of the two is a bug.

It answers the nine items of MRROIP-1.md §14.

| # | Item | `reference` |
| --- | --- | --- |
| 1 | `device_type`, `profile_version` | `reference`, `1.0` |
| 2 | `device_class` | Any of the three, **fixed when the endpoint starts** (a launch option or a build option), so one profile covers every branch of §11.3. `capabilities.autonomous` is true exactly when the class is `stationary` |
| 3 | Modes | `off`, `on`, `blink`. No mode takes a `target` |
| 4 | `busy` | True while blinking: in mode `blink`, or while the autonomous programme runs |
| 5 | Rest state | Mode `off`, lamp dark. `reset` and "come to rest" both go there |
| 6 | Autonomous programme | `stationary` only: repeat forever — blink `programme_blinks` times, then dark for one period. Each repetition counts one `blink_starts`. It starts at boot and whenever authority is released or times out |
| 7 | Faults | `simulated`: raised when `simulate_fault` is written 1; the lamp goes dark. Latched until `reset`, which clears it even if `simulate_fault` is still 1 (only a new 0→1 write raises it again) |
| 8 | `estop` | Lamp dark immediately, blinking and programme stopped; stays dark until `reset` |
| 9 | Tests | `probe/profile_reference.py`: hooks and P-1 … P-5 |

## Objects

```json
[
  { "id": "level", "type": "int", "min": 0, "max": 100, "default": 100, "unit": "%" },
  { "id": "blink_starts", "access": "state", "type": "int", "min": 0, "max": 2147483647 }
]
```

`level` is accepted with any of the three modes and applies to the lamp whenever it is lit. Refusals use
`invalid_object_state` with the reasons `unknown_object`, `wrong_type` and `out_of_range`.

## Parameters

| Name | Type | Range | Default | Unit |
| --- | --- | --- | --- | --- |
| `blink_period_ms` | int | 50 – 5000 | 500 | ms — one on/off cycle |
| `programme_blinks` | int | 1 – 100 | 4 | blinks per programme repetition |
| `simulate_fault` | int | 0 – 1 | 0 | — |

All three are persistable and apply immediately.

## State

`state.profile`:

```json
{ "lamp": "on", "level": 100, "blink_starts": 3 }
```

`lamp` is what the output is doing at this instant (`on` or `off`), so it alternates while blinking.
`blink_starts` counts every transition *into* blinking — entering mode `blink` from another mode, and
each repetition of the programme. A repeated `blink` adds nothing (§9.1). It is not persisted.

On an endpoint without a lamp — a host implementation — the lamp is a variable, and the implementation
MAY log its changes.
