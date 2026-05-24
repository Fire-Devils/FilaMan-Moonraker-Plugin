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
