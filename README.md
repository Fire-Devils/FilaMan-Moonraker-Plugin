# FilaMan Moonraker Driver Plugin

Driver plugin for FilaMan that forwards spool assignments to Moonraker's
`filaman` component.

## Features

- Set active spool in Moonraker via `/server/filaman/spool_id`
- Auto-discover toolheads from Moonraker printer objects
- Auto-discover tray/AMS slots when Moonraker exposes AFC/MMU data
- If Moonraker exposes no tray data, no tray slots are created
- Provide slot state in driver health for the existing spool assignment UI
- Optional macro execution per slot (`tray_macros` mode)
- Supports `assign_pending_spool()` for FilaMan auto-assign flows

## Files

- `moonraker_filaman/plugin.json`
- `moonraker_filaman/driver.py`

## Installation

1. Zip the folder `moonraker_filaman`.
2. Upload/install the zip in FilaMan plugin manager.
3. Create or edit a printer using driver key `moonraker_filaman`.

## Driver Config

```json
{
  "moonraker_url": "http://192.168.1.20:7125",
  "api_key": "",
  "mode": "toolhead_only",
  "request_timeout_seconds": 10,
  "slot_count": 1,
  "slot_targets": [
    {
      "slot_index": "0-0",
      "slot_name": "Toolhead 1",
      "slot_kind": "toolhead",
      "assign_gcode": "SET_TRAY_SPOOL TRAY=1 SPOOL={spool_id}"
    }
  ]
}
```

## Modes

- `toolhead_only` (default):
  - Assign action sets active spool in Moonraker only.
  - Slots are discovered from Moonraker (toolheads and available trays).
- `tray_macros`:
  - Assign action also runs `assign_gcode` for matching `slot_index`.

## Slot discovery

- Toolheads are discovered from Moonraker printer objects (`extruder`, `extruder1`, ...).
- Tray slots are discovered only when Moonraker exposes AFC/MMU objects or config.
- If no tray-related objects are present, only toolhead slots are shown.
- `slot_targets` overrides auto-discovery (manual mode).

## Auto-assign confirmation

When FilaMan arms a pending spool (e.g. after weighing a tagged spool on the scale), the driver
waits for the printer to confirm where that spool went.

- `auto_assign_confirm`
  - `sensor` (default) — wait for filament to appear. Two sources are watched: the toolhead
    filament sensor, and the per-slot presence reported by an AMS/MMU. The slot source is the
    useful one on a multi-slot machine: inserting a spool into a slot never reaches the toolhead
    sensor, and the slot that goes empty → present *identifies itself*, so the spool binds to the
    slot it was physically put in — no loading to the nozzle needed.
  - `immediate` — assign as soon as the spool is armed.
  - `off` — no pending auto-assign.
- `sensor_timeout_seconds` (default `300`) — how long to wait before dropping a pending assign.

### Slot presence source

Autodetected from `/printer/objects/list`; only set these to override.

| System | Object | Path | Values |
|---|---|---|---|
| QIDI BOX | `multi_color_controller` | `slots.states` | dict; `0` empty, `1` present, `2` loaded |
| Happy Hare | `mmu` | `gate_status` | list; `-1` unknown, `0` empty, `1`/`2` available |
| AFC | `AFC_stepper <lane>` (one per slot) | `prep` | boolean |

- `slot_sensor_object` — a single object, or a list of objects when there is one per slot.
- `slot_sensor_states_path` — dotted path to the presence data inside the object.
- `slot_sensor_per_slot` — set when the list is one object per slot rather than one object
  mapping all of them.

A slot counts as occupied when its value is a `true` boolean or a number **greater than zero**.
Plain truthiness is deliberately not used: Happy Hare reports `-1` for *unknown*, which would
otherwise read as occupied.

## Placeholders for `assign_gcode`

- `{spool_id}`
- `{slot_index}`
- `{ams_id}`
- `{tray_id}`
- `{material_type}`
- `{color}`

## Requirements

- Moonraker must be reachable from FilaMan backend.
- Moonraker should have the `filaman` component enabled.
