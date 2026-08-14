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
- `sensor_timeout_seconds` (default `300`) — how long a pending waits before it is dropped. A
  caller-supplied timeout is honoured when it is *longer*; this value is a floor, because a real
  spool change (heat up, unload, insert) outlasts a device-level auto-assign window.

On a printer that has tray slots, a toolhead-sensor edge alone never completes an assignment: a
toolhead-wide signal cannot say which tray the spool came from, so the pending is kept until a
slot sensor names one, or until it times out. The timeout is logged at warning level.

### Slot presence source

Autodetected from `/printer/objects/list`. Set these only to override it — and if you set the
object by hand you must set the path too, since detection fills in the pair or neither.

| System | Object | Path | Values |
|---|---|---|---|
| QIDI BOX | `multi_color_controller` | `slots.states` | dict; `0` empty, `1` present, `2` loaded |
| Happy Hare | `mmu` | `gate_status` | list; `-1` unknown, `0` empty, `1`/`2` available |
| AFC | `AFC_stepper <lane>`, one object per lane | `prep` | boolean |

- `slot_sensor_object` — a single object, or a list of objects when there is one per slot. Names
  are matched case-insensitively against the printer's object list; as an override, give the full
  name of each object (no wildcards).
- `slot_sensor_states_path` — dotted path to the presence data inside the object.
- `slot_sensor_per_slot` — set when the list is one object per slot rather than one object
  mapping all of them. Autodetection sets it for you.

A slot counts as occupied when its value is a `true` boolean or a number **greater than zero**.
Plain truthiness is deliberately not used: Happy Hare reports `-1` for *unknown*, which would
otherwise read as occupied. A value in any other shape (a string, say) drops that slot from the
map entirely rather than reporting it empty, so it can never produce a false insertion.

Slots are identified by number: the trailing digits of a key (`slot2` → `2`), the position in a
list, or the position in the object list when there is one object per lane. AFC lanes are commonly
named `lane1`…`lane4` while the trays they map to are indexed from zero, which is why the lane
*name* is not used for this.

**The number has to match a tray slot that this driver knows about**, otherwise the spool is not
assigned — deliberately, since guessing writes a wrong spool→slot binding that nothing later
corrects. Tray slots come from AFC/MMU objects, from `slot_targets`, or from `slot_count`. An AMS
that exposes neither AFC nor MMU objects — a QIDI BOX, for one — is invisible to discovery, so set
`slot_count` (or `slot_targets`) to the number of slots it has, or confirmation will refuse and
say so in the log.

### Diagnostics

Slot-sensor state is reported in the driver health payload (`slot_sensor_objects`,
`slot_sensor_states_path`, `slot_sensor_autodetected`, `slot_sensor_present`). A misconfigured
object or path warns once rather than every poll.

Note that on a printer with tray slots the driver does not report a printer-wide `active_spool_id`
from its status refresh — that value cannot name a tray, and reporting it caused it to be recorded
against the first slot. It is still present in the health payload.

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
