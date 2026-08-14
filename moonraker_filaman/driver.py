import asyncio
import contextlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from sqlalchemy import select

from app.core.database import async_session_maker
from app.models.location import Location
from app.models.printer import Printer
from app.models.printer_params import SpoolPrinterParam
from app.models.spool import Spool
from app.plugins.base import BaseDriver
from app.services.spool_service import SpoolService

logger = logging.getLogger(__name__)

# Per-slot presence as reported by the AMS/MMU firmwares this plugin targets.
# (object glob, path within the object, one object per slot?)
# Values are read with _slot_sensor_present_value(): numbers > 0 mean present,
# booleans are taken as-is. That is deliberate — Happy Hare uses -1 for
# "unknown", which truthiness would report as occupied.
SLOT_SENSOR_PRESETS: list[dict[str, Any]] = [
    # QIDI BOX: {"slot0": 2, "slot1": 1, ...}; 0 empty, 1 present, 2 loaded.
    {
        "match": "multi_color_controller",
        "per_slot": False,
        "path": "slots.states",
    },
    # Happy Hare: gate_status is a LIST indexed by gate.
    # -1 unknown, 0 empty, 1 available, 2 available from buffer.
    {
        "match": "mmu",
        "per_slot": False,
        "path": "gate_status",
    },
    # AFC: one object per lane, each with a boolean `prep`.
    {
        "match": "AFC_stepper ",
        "per_slot": True,
        "path": "prep",
    },
]


_ORIGINAL_LOCATION_PARAM_KEY = "moonraker_original_location_id"
_NONE_LOCATION_SENTINEL = "__none__"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Driver(BaseDriver):
    driver_key = "moonraker_filaman"

    def __init__(
        self,
        printer_id: int,
        config: dict[str, Any],
        emitter: Callable[[dict[str, Any]], None],
    ):
        super().__init__(printer_id, config, emitter)
        self._moonraker_url = (config.get("moonraker_url") or "").rstrip("/")
        self._api_key = config.get("api_key") or ""
        self._mode = config.get("mode", "toolhead_only")
        self._request_timeout = int(config.get("request_timeout_seconds", 10))
        self._slot_targets = self._normalize_slot_targets(config)
        self._slot_count = int(config.get("slot_count", 1))
        confirm = str(config.get("auto_assign_confirm", "sensor")).strip().lower()
        if confirm not in ("sensor", "immediate", "off"):
            confirm = "sensor"
        self._auto_assign_confirm = confirm
        self._sensor_timeout = int(config.get("sensor_timeout_seconds", 300))
        # Slot sensor confirmation: watch per-slot filament presence reported by
        # the AMS/MMU itself, so inserting a spool into a SLOT — not only loading
        # it to the nozzle — confirms a pending auto-assign, and the slot that
        # lights up names itself.
        #
        # slot_sensor_object accepts either one Klipper object holding a map of
        # all slots, or a list of objects, one per slot. Left unset, it is
        # autodetected from /printer/objects/list at start(); see
        # SLOT_SENSOR_PRESETS.
        raw_objects = config.get("slot_sensor_object")
        if isinstance(raw_objects, str):
            raw_objects = [raw_objects]
        elif not isinstance(raw_objects, list):
            raw_objects = []
        self._slot_sensor_objects: list[str] = [
            str(name).strip() for name in raw_objects if str(name).strip()
        ]
        self._slot_sensor_states_path = str(
            config.get("slot_sensor_states_path", "") or ""
        ).strip()
        self._slot_sensor_autodetected = False

        self._slots: list[dict[str, Any]] = self._build_initial_slots()
        self._active_spool_id: int | None = None
        self._pending: dict[str, Any] | None = None
        self._pending_timer: asyncio.Task | None = None
        self._filament_present: dict[str, Any] = {}
        self._slot_sensor_present: dict[str, bool] = {}
        # True when slot_sensor_object lists one object PER SLOT
        # (AFC), False when a single object holds a map of all of them
        # (QIDI, Happy Hare). Autodetection sets it; configuring the
        # objects by hand must be able to say so too.
        self._slot_sensor_per_slot: bool = bool(
            config.get('slot_sensor_per_slot', False)
        )
        self._lock = asyncio.Lock()

        self._connected = False
        self._last_error: str | None = None
        self._last_success_at: str | None = None
        self._poll_task: asyncio.Task | None = None
        self._status_poll_interval = 5
        self._discovery_interval_seconds = 60
        self._discovery_ticks = max(
            1, self._discovery_interval_seconds // self._status_poll_interval
        )
        self._poll_ticks = 0
        self._slot_to_filaman_spool: dict[str, int] = {}
        self._spool_original_location: dict[int, int | None] = {}
        self._spool_original_known: set[int] = set()
        self._printer_name: str | None = None

    def _emit_slots_update(self) -> None:
        self.emit(
            {
                "event_type": "slots_update",
                "slots": self._slots,
                "ams_info": self._build_ams_info(),
                "active_spool_id": self._active_spool_id,
            }
        )

    def _normalize_slot_targets(self, config: dict[str, Any]) -> list[dict[str, Any]]:
        raw = config.get("slot_targets")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            slot_index = str(item.get("slot_index", "")).strip()
            slot_name = str(item.get("slot_name", "")).strip()
            if not slot_index or not slot_name:
                continue
            out.append(
                {
                    "slot_index": slot_index,
                    "slot_name": slot_name,
                    "slot_kind": str(item.get("slot_kind", "tray") or "tray").strip(),
                    "assign_gcode": str(item.get("assign_gcode", "") or "").strip(),
                    "default_material_type": str(
                        item.get("default_material_type", "PLA") or "PLA"
                    ).strip(),
                    "default_color": str(item.get("default_color", "FFFFFF") or "FFFFFF")
                    .replace("#", "")
                    .upper()[:6],
                }
            )
        return out

    def _build_initial_slots(self) -> list[dict[str, Any]]:
        if self._slot_targets:
            return [
                {
                    "slot_index": target["slot_index"],
                    "slot_name": target["slot_name"],
                    "slot_kind": target.get("slot_kind", "tray"),
                    "tray_info_idx": "",
                    "tray_type": "",
                    "tray_color": "",
                    "present": False,
                }
                for target in self._slot_targets
            ]

        # Fallback only: provide toolheads when discovery is not yet available.
        return [
            {
                "slot_index": f"0-{idx}",
                "slot_name": f"Toolhead {idx + 1}",
                "slot_kind": "toolhead",
                "tray_info_idx": "",
                "tray_type": "",
                "tray_color": "",
                "present": False,
                "spool_id": None,
            }
            for idx in range(max(1, self._slot_count))
        ]

    @staticmethod
    def _unwrap_result(payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("result")
        if isinstance(result, dict):
            return result
        return payload

    @staticmethod
    def _slot_to_ids(slot_index: str) -> tuple[int, int]:
        parts = slot_index.split("-", 1)
        if len(parts) != 2:
            return 0, 0
        try:
            return int(parts[0]), int(parts[1])
        except Exception:
            return 0, 0

    def _slot_extruder(self, slot: dict[str, Any] | None) -> str | None:
        """Klipper extruder name for a toolhead slot; None for tray slots."""
        if not slot or str(slot.get("slot_kind") or "") != "toolhead":
            return None
        name = str(slot.get("toolhead_name") or "").strip()
        if name:
            return name
        _unit, idx = self._slot_to_ids(str(slot.get("slot_index", "0-0")))
        return "extruder" if idx == 0 else f"extruder{idx}"

    def _find_slot(self, slot_index: str) -> dict[str, Any] | None:
        return next(
            (item for item in self._slots if item.get("slot_index") == slot_index),
            None,
        )

    @staticmethod
    def _dig(obj: Any, path: str) -> Any:
        """Walk a dotted path (e.g. 'slots.states') through nested dicts."""
        cur = obj
        for part in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    @staticmethod
    def _slot_sensor_present_value(value: Any) -> bool | None:
        """Presence from one raw sensor value; None when it says nothing.

        Booleans are taken as-is. Numbers are present when > 0, which is what
        every supported firmware means: QIDI 1/2 occupied, Happy Hare 1/2
        available while -1 is *unknown* and 0 is empty. Plain truthiness would
        turn that -1 into "occupied".
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value > 0
        return None

    def _sensor_key_to_slot_index(self, sensor_key: str) -> str | None:
        """Map a sensor key ('slot3', 'gate3', 'AFC_stepper lane3', '3') to '0-3'."""
        match = re.search(r"(\d+)\s*$", str(sensor_key))
        if not match:
            return None
        n = match.group(1)
        for slot in self._slots:
            idx = str(slot.get("slot_index", ""))
            if idx.endswith(f"-{n}") and str(slot.get("slot_kind") or "") == "tray":
                return idx
        return f"0-{n}"

    async def _autodetect_slot_sensor(self) -> None:
        """Pick a slot-presence source from the objects the printer exposes.

        Only when the user has not configured one. Re-runs on every start so a
        firmware or add-on change is picked up without editing the config.
        """
        if self._slot_sensor_objects and not self._slot_sensor_autodetected:
            return
        try:
            objects = await self._discover_objects()
        except Exception:
            return

        for preset in SLOT_SENSOR_PRESETS:
            needle = str(preset["match"])
            if preset["per_slot"]:
                found = sorted(name for name in objects if name.startswith(needle))
            else:
                found = [name for name in objects if name == needle]
            if not found:
                continue
            self._slot_sensor_objects = found
            self._slot_sensor_per_slot = bool(preset["per_slot"])
            if not self._slot_sensor_states_path:
                self._slot_sensor_states_path = str(preset["path"])
            self._slot_sensor_autodetected = True
            logger.info(
                "Slot sensor autodetected for printer %s: %s (path '%s')",
                self.printer_id,
                found if len(found) < 5 else f"{len(found)} objects",
                self._slot_sensor_states_path,
            )
            return

    async def _read_slot_sensor(self) -> dict[str, bool]:
        """Current per-slot presence, keyed by whatever the firmware calls a slot."""
        if not self._slot_sensor_objects:
            return {}
        query = "&".join(
            name.replace(" ", "%20") for name in self._slot_sensor_objects
        )
        try:
            payload = await self._request("GET", f"/printer/objects/query?{query}")
        except Exception:
            return {}
        status = self._unwrap_result(payload).get("status")
        if not isinstance(status, dict):
            return {}

        out: dict[str, bool] = {}
        for name in self._slot_sensor_objects:
            obj = status.get(name)
            if not isinstance(obj, dict):
                continue
            raw = (
                self._dig(obj, self._slot_sensor_states_path)
                if self._slot_sensor_states_path
                else obj
            )

            if getattr(self, "_slot_sensor_per_slot", False):
                # One object per slot: the whole object is a single slot's state.
                present = self._slot_sensor_present_value(raw)
                if present is not None:
                    out[name] = present
                continue

            if isinstance(raw, dict):
                for key, value in raw.items():
                    present = self._slot_sensor_present_value(value)
                    if present is not None:
                        out[str(key)] = present
            elif isinstance(raw, (list, tuple)):
                # Indexed by slot number (Happy Hare gate_status).
                for index, value in enumerate(raw):
                    present = self._slot_sensor_present_value(value)
                    if present is not None:
                        out[str(index)] = present
        return out

    async def _poll_slot_sensor(self) -> None:
        """Confirm a pending assignment when a slot goes empty -> present.

        Complements the toolhead filament sensor: inserting a spool into an AMS
        slot never reaches the nozzle sensor, but the AMS's own per-slot presence
        does change, and the slot that lights up identifies itself.
        """
        current = await self._read_slot_sensor()
        if not current:
            return

        previous = self._slot_sensor_present
        self._slot_sensor_present = current

        if self._pending is None or self._auto_assign_confirm != "sensor":
            return
        for sensor_key, present in current.items():
            # Fire only on a real empty->present edge we have a baseline for, so
            # pre-existing occupancy on the first poll never counts as insertion.
            if present and previous.get(sensor_key) is False:
                slot_index = self._sensor_key_to_slot_index(sensor_key)
                if slot_index:
                    logger.info(
                        "Slot %s went present -> confirming pending spool on "
                        "%s (printer %s)",
                        sensor_key,
                        slot_index,
                        self.printer_id,
                    )
                    await self._complete_pending_assignment(slot_index=slot_index)
                    break

    def _apply_extruder_spools(self, extruder_spools: dict[str, Any]) -> bool:
        """Mirror Moonraker's per-extruder spool map onto the toolhead slots."""
        changed = False
        for slot in self._slots:
            extruder = self._slot_extruder(slot)
            if extruder is None or extruder not in extruder_spools:
                continue

            raw = extruder_spools.get(extruder)
            spool_id: int | None = None
            if raw is not None and not isinstance(raw, bool):
                with contextlib.suppress(TypeError, ValueError):
                    spool_id = int(raw)

            if slot.get("spool_id") == spool_id:
                continue

            slot["spool_id"] = spool_id
            slot["present"] = spool_id is not None
            if spool_id is None:
                slot.update({"tray_type": "", "tray_color": "", "tray_info_idx": ""})
            changed = True
        return changed

    async def _discover_objects(self) -> list[str]:
        payload = await self._request("GET", "/printer/objects/list")
        data = self._unwrap_result(payload)
        objects = data.get("objects")
        if not isinstance(objects, list):
            return []
        return [str(item) for item in objects]

    async def _discover_tray_count_from_config(self) -> int:
        try:
            payload = await self._request("GET", "/server/config")
            data = self._unwrap_result(payload)
            orig = data.get("orig")
            if not isinstance(orig, dict):
                return 0

            section_names = [str(name) for name in orig.keys()]
            afc_sections = [name for name in section_names if name.startswith("afc_stepper ")]
            if afc_sections:
                return len(afc_sections)

            mmu_gate_sections = [
                name for name in section_names if name.startswith("mmu_gate ")
            ]
            if mmu_gate_sections:
                return len(mmu_gate_sections)

            mmu_cfg = orig.get("mmu")
            if isinstance(mmu_cfg, dict):
                raw_gates = mmu_cfg.get("num_gates")
                if raw_gates is not None:
                    with contextlib.suppress(Exception):
                        gates = int(str(raw_gates).strip())
                        if gates > 0:
                            return gates
        except Exception:
            return 0

        return 0

    async def _discover_slots(self) -> list[dict[str, Any]]:
        if self._slot_targets:
            return self._build_initial_slots()

        objects = await self._discover_objects()

        extruder_names = sorted(
            name for name in objects if re.match(r"^extruder\d*$", name)
        )
        if "extruder" in extruder_names:
            extruder_names.remove("extruder")
            extruder_names.insert(0, "extruder")

        if not extruder_names:
            extruder_names = [f"extruder{idx}" if idx else "extruder" for idx in range(max(1, self._slot_count))]

        discovered: list[dict[str, Any]] = []

        for idx, name in enumerate(extruder_names):
            discovered.append(
                {
                    "slot_index": f"0-{idx}",
                    "slot_name": f"Toolhead {idx + 1}",
                    "slot_kind": "toolhead",
                    "toolhead_name": name,
                    "tray_info_idx": "",
                    "tray_type": "",
                    "tray_color": "",
                    "present": False,
                    "spool_id": None,
                }
            )

        tray_count = 0
        afc_object_count = len([name for name in objects if name.startswith("afc_stepper ")])
        mmu_object_count = len([name for name in objects if name.startswith("mmu_gate ")])
        tray_count = max(afc_object_count, mmu_object_count)
        if tray_count == 0:
            tray_count = await self._discover_tray_count_from_config()

        for idx in range(tray_count):
            discovered.append(
                {
                    "slot_index": f"1-{idx}",
                    "slot_name": f"Tray {idx + 1}",
                    "slot_kind": "tray",
                    "tray_info_idx": "",
                    "tray_type": "",
                    "tray_color": "",
                    "present": False,
                }
            )

        return discovered

    async def _refresh_slots(self, emit: bool = True) -> None:
        try:
            discovered = await self._discover_slots()
        except Exception as exc:
            logger.debug("slot discovery failed for printer %s: %s", self.printer_id, exc)
            return

        if not discovered:
            return

        old_map = {slot.get("slot_index"): slot for slot in self._slots}
        merged: list[dict[str, Any]] = []
        for slot in discovered:
            slot_index = slot.get("slot_index")
            old_slot = old_map.get(slot_index)
            if old_slot:
                for key in ("tray_info_idx", "tray_type", "tray_color", "present"):
                    if old_slot.get(key) not in (None, "", False):
                        slot[key] = old_slot[key]
                # Carried over unconditionally: None is a meaningful value here
                # (slot explicitly unassigned), unlike the tray metadata above.
                slot["spool_id"] = old_slot.get("spool_id")
            merged.append(slot)

        changed = [slot.get("slot_index") for slot in merged] != [
            slot.get("slot_index") for slot in self._slots
        ]
        self._slots = merged

        if emit and changed:
            self._emit_slots_update()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            # Only X-Api-Key: Moonraker treats Authorization: Bearer as a JWT and
            # fails to decode a plain API key, returning a false 401.
            headers["X-Api-Key"] = self._api_key
        return headers

    def _validate_url(self) -> None:
        parsed = urlparse(self._moonraker_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("moonraker_url must be a valid http(s) URL")

    def validate_config(self) -> None:
        self._validate_url()
        if self._mode not in {"toolhead_only", "tray_macros"}:
            raise ValueError("mode must be one of: toolhead_only, tray_macros")

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import aiohttp

        url = f"{self._moonraker_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self._request_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method,
                url,
                headers=self._headers(),
                json=body,
            ) as response:
                text = await response.text()
                payload: dict[str, Any] = {}
                if text:
                    try:
                        payload = json.loads(text)
                    except Exception:
                        payload = {"raw": text}

                if response.status >= 400:
                    message = (
                        payload.get("error", {}).get("message")
                        or payload.get("detail", {}).get("message")
                        or payload.get("message")
                        or text
                    )
                    raise RuntimeError(f"Moonraker HTTP {response.status}: {message}")

                return payload

    async def _set_moonraker_active_spool(
        self,
        spool_id: int | None,
        extruder: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"spool_id": spool_id}
        if extruder:
            # Without this the component falls back to whichever extruder is
            # currently active in Klipper, so the assignment lands on the wrong
            # toolhead.
            body["extruder"] = extruder
        await self._request("POST", "/server/filaman/spool_id", body)
        if extruder is None:
            self._active_spool_id = spool_id

    def _render_gcode(
        self,
        template: str,
        spool_id: int,
        ams_id: int,
        tray_id: int,
        slot_index: str,
        filament_data: dict[str, Any],
    ) -> str:
        material_type = str(filament_data.get("material_type", "PLA"))
        color = str(filament_data.get("color", "FFFFFF")).replace("#", "")[:6]
        return template.format(
            spool_id=spool_id,
            ams_id=ams_id,
            tray_id=tray_id,
            slot_index=slot_index,
            material_type=material_type,
            color=color,
        )

    async def _execute_assign_macro(
        self,
        spool_id: int,
        ams_id: int,
        tray_id: int,
        slot_index: str,
        filament_data: dict[str, Any],
    ) -> None:
        if self._mode != "tray_macros":
            return
        target = next(
            (item for item in self._slot_targets if item["slot_index"] == slot_index),
            None,
        )
        if not target:
            return
        assign_gcode = target.get("assign_gcode")
        if not assign_gcode:
            return

        script = self._render_gcode(
            template=assign_gcode,
            spool_id=spool_id,
            ams_id=ams_id,
            tray_id=tray_id,
            slot_index=slot_index,
            filament_data=filament_data,
        )
        await self._request("POST", "/printer/gcode/script", {"script": script})

    async def _mark_slot(
        self,
        slot_index: str,
        filament_data: dict[str, Any],
        spool_id: int | None = None,
    ) -> None:
        tray_type = str(filament_data.get("material_type") or "PLA")
        tray_color = str(filament_data.get("color") or "FFFFFF").replace("#", "")[:6]
        tray_info_idx = str(filament_data.get("tray_info_idx") or "FILAMAN")

        marked = {
            "tray_type": tray_type,
            "tray_color": tray_color,
            "tray_info_idx": tray_info_idx,
            "present": True,
            "spool_id": spool_id,
        }

        slot = self._find_slot(slot_index)
        if slot is not None:
            slot.update(marked)
        else:
            self._slots.append(
                {
                    "slot_index": slot_index,
                    "slot_name": slot_index,
                    **marked,
                }
            )

        self._emit_slots_update()

    def _build_ams_info(self) -> dict[str, Any]:
        tray_slots = [slot for slot in self._slots if slot.get("slot_kind") == "tray"]
        tray_groups: dict[int, int] = {}
        for slot in tray_slots:
            ams_id, _tray_id = self._slot_to_ids(str(slot.get("slot_index", "0-0")))
            tray_groups[ams_id] = tray_groups.get(ams_id, 0) + 1

        return {
            "ams_count": len(tray_groups),
            "ams_type": "Moonraker",
            "slot_count": len(self._slots),
            "external_spool": False,
            "ams_units": [
                {
                    "ams_id": ams_id,
                    "tray_count": tray_count,
                    "humidity": None,
                    "temp": None,
                    "serial": "moonraker",
                }
                for ams_id, tray_count in sorted(tray_groups.items())
            ],
        }

    async def _load_printer_name(self) -> None:
        try:
            async with async_session_maker() as db:
                printer = await db.get(Printer, self.printer_id)
                if printer and printer.name:
                    self._printer_name = str(printer.name).strip()
                else:
                    self._printer_name = f"Printer {self.printer_id}"
        except Exception as exc:
            logger.warning(
                "Could not load printer name for moonraker printer %s: %s",
                self.printer_id,
                exc,
            )
            self._printer_name = f"Printer {self.printer_id}"

    @staticmethod
    def _location_value_to_param(location_id: int | None) -> str:
        if location_id is None:
            return _NONE_LOCATION_SENTINEL
        return str(location_id)

    @staticmethod
    def _location_param_to_value(raw: str | None) -> int | None:
        if raw is None:
            return None
        value = str(raw).strip()
        if not value or value == _NONE_LOCATION_SENTINEL:
            return None
        try:
            return int(value)
        except Exception:
            return None

    async def _load_original_location_cache(self) -> None:
        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(SpoolPrinterParam).where(
                        SpoolPrinterParam.printer_id == self.printer_id,
                        SpoolPrinterParam.param_key == _ORIGINAL_LOCATION_PARAM_KEY,
                    )
                )
                params = result.scalars().all()
                for param in params:
                    self._spool_original_known.add(param.spool_id)
                    self._spool_original_location[param.spool_id] = (
                        self._location_param_to_value(param.param_value)
                    )
        except Exception as exc:
            logger.warning(
                "Could not restore original spool locations for printer %s: %s",
                self.printer_id,
                exc,
            )

    async def _store_original_location_db(
        self,
        spool_id: int,
        location_id: int | None,
    ) -> None:
        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(SpoolPrinterParam)
                    .where(
                        SpoolPrinterParam.spool_id == spool_id,
                        SpoolPrinterParam.printer_id == self.printer_id,
                        SpoolPrinterParam.param_key == _ORIGINAL_LOCATION_PARAM_KEY,
                    )
                    .limit(1)
                )
                existing = result.scalars().first()
                param_value = self._location_value_to_param(location_id)

                if existing:
                    existing.param_value = param_value
                else:
                    db.add(
                        SpoolPrinterParam(
                            spool_id=spool_id,
                            printer_id=self.printer_id,
                            param_key=_ORIGINAL_LOCATION_PARAM_KEY,
                            param_value=param_value,
                        )
                    )

                await db.commit()
        except Exception as exc:
            logger.warning(
                "Failed to persist original location for spool %s: %s",
                spool_id,
                exc,
            )

    async def _delete_original_location_db(self, spool_id: int) -> None:
        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(SpoolPrinterParam)
                    .where(
                        SpoolPrinterParam.spool_id == spool_id,
                        SpoolPrinterParam.printer_id == self.printer_id,
                        SpoolPrinterParam.param_key == _ORIGINAL_LOCATION_PARAM_KEY,
                    )
                    .limit(1)
                )
                existing = result.scalars().first()
                if not existing:
                    return
                await db.delete(existing)
                await db.commit()
        except Exception as exc:
            logger.warning(
                "Failed to delete original location for spool %s: %s",
                spool_id,
                exc,
            )

    async def _cache_original_location(self, spool_id: int) -> None:
        if spool_id in self._spool_original_known:
            return

        location_id: int | None = None
        try:
            async with async_session_maker() as db:
                result = await db.execute(
                    select(SpoolPrinterParam)
                    .where(
                        SpoolPrinterParam.spool_id == spool_id,
                        SpoolPrinterParam.printer_id == self.printer_id,
                        SpoolPrinterParam.param_key == _ORIGINAL_LOCATION_PARAM_KEY,
                    )
                    .limit(1)
                )
                existing = result.scalars().first()

                if existing:
                    location_id = self._location_param_to_value(existing.param_value)
                else:
                    spool = await db.get(Spool, spool_id)
                    if spool is None:
                        logger.warning(
                            "Could not cache original location, spool %s not found",
                            spool_id,
                        )
                        return
                    location_id = spool.location_id
                    db.add(
                        SpoolPrinterParam(
                            spool_id=spool_id,
                            printer_id=self.printer_id,
                            param_key=_ORIGINAL_LOCATION_PARAM_KEY,
                            param_value=self._location_value_to_param(location_id),
                        )
                    )
                    await db.commit()
        except Exception as exc:
            logger.warning(
                "Failed to cache original location for spool %s: %s",
                spool_id,
                exc,
            )
            return

        self._spool_original_known.add(spool_id)
        self._spool_original_location[spool_id] = location_id

    def _slot_location_identifier(self, slot_index: str) -> str:
        safe_slot = re.sub(r"[^A-Za-z0-9_-]", "_", slot_index).strip("_")
        if not safe_slot:
            safe_slot = "slot"
        return f"moonraker_{self.printer_id}_{safe_slot}"[:100]

    def _slot_display_name(self, slot_index: str) -> str:
        slot = next(
            (item for item in self._slots if item.get("slot_index") == slot_index),
            None,
        )
        if slot and slot.get("slot_name"):
            return str(slot["slot_name"])
        return slot_index

    def _slot_location_name(self, slot_index: str) -> str:
        printer_name = self._printer_name or f"Printer {self.printer_id}"
        slot_name = self._slot_display_name(slot_index)
        return f"{printer_name} - {slot_name}"

    def _find_slot_for_spool(self, spool_id: int) -> str | None:
        for slot_index, mapped_spool_id in self._slot_to_filaman_spool.items():
            if mapped_spool_id == spool_id:
                return slot_index
        return None

    def _slot_spool_map(self) -> dict[str, int]:
        """Current spool ownership per slot, as reported by the printer."""
        out: dict[str, int] = {}
        for slot in self._slots:
            spool_id = slot.get("spool_id")
            if isinstance(spool_id, int) and not isinstance(spool_id, bool):
                out[str(slot.get("slot_index"))] = spool_id
        return out

    async def _reconcile_slot_locations(self) -> None:
        """Align spool locations with the per-slot spool map.

        Driven by the slots themselves rather than the printer-wide active
        spool, which cannot express which toolhead a spool belongs to.
        Caller must hold self._lock.
        """
        desired = self._slot_spool_map()
        still_mounted = set(desired.values())

        for slot_index, spool_id in list(self._slot_to_filaman_spool.items()):
            if desired.get(slot_index) == spool_id:
                continue
            self._slot_to_filaman_spool.pop(slot_index, None)
            # Only send it home if it hasn't merely moved to another slot.
            if spool_id not in still_mounted:
                await self._restore_spool_location(spool_id)

        for slot_index, spool_id in desired.items():
            if self._slot_to_filaman_spool.get(slot_index) == spool_id:
                continue
            await self._cache_original_location(spool_id)
            await self._move_spool_to_slot_location(spool_id, slot_index)
            self._slot_to_filaman_spool[slot_index] = spool_id

    async def _move_spool_to_slot_location(self, spool_id: int, slot_index: str) -> None:
        slot_location_name = self._slot_location_name(slot_index)
        slot_location_identifier = self._slot_location_identifier(slot_index)

        try:
            async with async_session_maker() as db:
                spool = await db.get(Spool, spool_id)
                if spool is None:
                    logger.warning(
                        "Spool %s not found while updating Moonraker location",
                        spool_id,
                    )
                    return

                result = await db.execute(
                    select(Location)
                    .where(Location.identifier == slot_location_identifier)
                    .order_by(Location.id.asc())
                    .limit(1)
                )
                location = result.scalars().first()

                location_changed = False
                desired_custom_fields = {
                    "managed_by": "moonraker_filaman",
                    "printer_id": self.printer_id,
                    "slot_index": slot_index,
                }

                if location is None:
                    location = Location(
                        name=slot_location_name,
                        identifier=slot_location_identifier,
                        custom_fields=desired_custom_fields,
                    )
                    db.add(location)
                    await db.flush()
                    location_changed = True
                else:
                    if location.name != slot_location_name:
                        location.name = slot_location_name
                        location_changed = True

                    if location.identifier != slot_location_identifier:
                        location.identifier = slot_location_identifier
                        location_changed = True

                    merged_custom_fields = {
                        **(location.custom_fields or {}),
                        **desired_custom_fields,
                    }
                    if merged_custom_fields != (location.custom_fields or {}):
                        location.custom_fields = merged_custom_fields
                        location_changed = True

                if location.id is None:
                    logger.warning(
                        "Moonraker slot location has no id for printer %s slot %s",
                        self.printer_id,
                        slot_index,
                    )
                    return

                if spool.location_id == location.id:
                    if location_changed:
                        await db.commit()
                    return

                await SpoolService(db).move_location(
                    spool,
                    location.id,
                    datetime.now(timezone.utc),
                    source="driver",
                    note=f"Assigned to {slot_location_name}",
                )
        except Exception as exc:
            logger.error(
                "Failed to move spool %s to Moonraker slot %s: %s",
                spool_id,
                slot_index,
                exc,
                exc_info=True,
            )

    async def _restore_slots_from_locations(self) -> None:
        """Rebuild per-slot spool ownership from persisted spool locations.

        Slot marks live in process memory, so a restart blanks them while the
        database still knows where every spool sits. Read that back, otherwise
        the first _emit_slots_update() persists four empty slots over the truth.
        """
        try:
            async with async_session_maker() as db:
                for slot in self._slots:
                    slot_index = str(slot.get("slot_index") or "")
                    if not slot_index or slot.get("spool_id") is not None:
                        continue

                    identifier = self._slot_location_identifier(slot_index)
                    query = (
                        select(Spool)
                        .join(Location, Spool.location_id == Location.id)
                        .where(Location.identifier == identifier)
                        .order_by(Spool.id.desc())
                        .limit(1)
                    )
                    archived = getattr(Spool, "archived", None)
                    if archived is not None:
                        query = query.where(archived.is_(False))

                    spool = (await db.execute(query)).scalars().first()
                    if spool is None:
                        continue

                    slot["present"] = True
                    slot["spool_id"] = spool.id
                    self._slot_to_filaman_spool[slot_index] = spool.id
                    logger.info(
                        "Restored spool %s into slot %s from its location "
                        "(printer %s)",
                        spool.id,
                        slot_index,
                        self.printer_id,
                    )
        except Exception:
            logger.warning(
                "Could not restore slot assignments from locations for "
                "printer %s",
                self.printer_id,
                exc_info=True,
            )

    async def _restore_spool_location(self, spool_id: int) -> None:
        if spool_id not in self._spool_original_known:
            return

        if spool_id in self._slot_to_filaman_spool.values():
            return

        original_location_id = self._spool_original_location.get(spool_id)

        try:
            async with async_session_maker() as db:
                spool = await db.get(Spool, spool_id)
                if spool is None:
                    logger.warning(
                        "Spool %s not found while restoring Moonraker location",
                        spool_id,
                    )
                elif spool.location_id != original_location_id:
                    await SpoolService(db).move_location(
                        spool,
                        original_location_id,
                        datetime.now(timezone.utc),
                        source="driver",
                        note="Removed from Moonraker slot",
                    )

            await self._delete_original_location_db(spool_id)
            self._spool_original_known.discard(spool_id)
            self._spool_original_location.pop(spool_id, None)
        except Exception as exc:
            logger.warning(
                "Failed to restore original location for spool %s: %s",
                spool_id,
                exc,
            )

    async def start(self) -> None:
        self._validate_url()
        self._running = True
        await self._load_printer_name()
        await self._load_original_location_cache()

        if self._mode == "tray_macros" and not self._slot_targets:
            logger.warning(
                "moonraker_filaman printer %s runs in tray_macros mode without slot_targets",
                self.printer_id,
            )

        # Initial status check
        initial_extruder_spools: dict[str, Any] = {}
        try:
            payload = await self._request("GET", "/server/filaman/status")
            result = self._unwrap_result(payload)
            self._connected = bool(result.get("filaman_connected", True))
            self._active_spool_id = result.get("spool_id")
            if isinstance(result.get("extruder_spools"), dict):
                initial_extruder_spools = result["extruder_spools"]
            self._last_error = None
            self._last_success_at = _now_iso()
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)

        await self._refresh_slots(emit=False)
        # After discovery, so the toolhead slots exist to be filled in.
        self._apply_extruder_spools(initial_extruder_spools)

        # Slot marks are in-memory only; a restart would otherwise publish four
        # empty slots and overwrite the persisted assignments.
        await self._restore_slots_from_locations()

        # Pick a slot-presence source unless one is configured, then seed the
        # baseline so the first real insertion reads as an empty->present edge
        # rather than pre-existing occupancy.
        with contextlib.suppress(Exception):
            await self._autodetect_slot_sensor()
        with contextlib.suppress(Exception):
            await self._poll_slot_sensor()

        async with self._lock:
            await self._reconcile_slot_locations()

        self._emit_slots_update()
        self._poll_task = asyncio.create_task(self._poll_status_loop())
        logger.info(
            "Moonraker FilaMan driver started for printer %s (%s)",
            self.printer_id,
            self._moonraker_url,
        )

    async def stop(self) -> None:
        self._running = False
        if self._pending_timer and not self._pending_timer.done():
            self._pending_timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pending_timer
        self._pending_timer = None
        self._pending = None

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        self._poll_task = None

        self._connected = False

    async def reconnect(self) -> None:
        await self.stop()
        await self.start()

    async def _poll_status_loop(self) -> None:
        while self._running:
            try:
                await self.refresh_status()
                await self._poll_slot_sensor()

                if not self._slot_targets:
                    self._poll_ticks += 1
                    if self._poll_ticks >= self._discovery_ticks:
                        self._poll_ticks = 0
                        await self._refresh_slots(emit=True)
            except Exception as exc:
                self._connected = False
                self._last_error = str(exc)
            await asyncio.sleep(self._status_poll_interval)

    async def refresh_status(self) -> dict[str, Any]:
        payload = await self._request("GET", "/server/filaman/status")
        result = self._unwrap_result(payload)

        previous_spool_id = self._active_spool_id
        self._connected = bool(result.get("filaman_connected", False))
        self._active_spool_id = result.get("spool_id")
        self._last_error = result.get("last_error")
        self._last_success_at = result.get("last_success_at") or _now_iso()

        extruder_spools = result.get("extruder_spools")
        if not isinstance(extruder_spools, dict):
            extruder_spools = {}
        slots_changed = self._apply_extruder_spools(extruder_spools)

        if slots_changed:
            async with self._lock:
                await self._reconcile_slot_locations()

        if slots_changed or previous_spool_id != self._active_spool_id:
            self._emit_slots_update()

        filament_present = result.get("filament_present")
        if not isinstance(filament_present, dict):
            filament_present = {}
        previous_present = self._filament_present
        self._filament_present = dict(filament_present)

        if self._pending is not None and self._auto_assign_confirm == "sensor":
            for extruder, present in filament_present.items():
                if present and not previous_present.get(extruder):
                    await self._complete_pending_assignment(extruder)
                    break

        # Both consumers of this return value feed active_spool_id into
        # _handle_slots_update() with an EMPTY slot list, which stamps it onto
        # slot 0-0. Harmless for a toolhead-only printer, wrong for a tray box:
        # it overwrites the real per-slot map on every health refresh.
        has_tray_slots = any(
            str(slot.get("slot_kind") or "") == "tray" for slot in self._slots
        )

        return {
            "connected": self._connected,
            "active_spool_id": None if has_tray_slots else self._active_spool_id,
            "extruder_spools": extruder_spools,
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
        }

    async def send_filament_to_tray(
        self,
        ams_id: int,
        tray_id: int,
        filament_data: dict,
        spool_id: int | None = None,
    ) -> None:
        if spool_id is None:
            spool_id = filament_data.get("id")

        if isinstance(spool_id, bool) or not isinstance(spool_id, int):
            raise ValueError("spool_id must be an integer")

        if not isinstance(ams_id, int) or not isinstance(tray_id, int):
            raise ValueError("ams_id and tray_id must be integers")

        slot_index = f"{ams_id}-{tray_id}"
        slot = self._find_slot(slot_index)
        if slot is None:
            raise ValueError(f"Unknown slot index '{slot_index}'")

        async with self._lock:
            old_slot_for_spool = self._find_slot_for_spool(spool_id)
            if old_slot_for_spool and old_slot_for_spool != slot_index:
                self._slot_to_filaman_spool.pop(old_slot_for_spool, None)

            replaced_spool_id = self._slot_to_filaman_spool.get(slot_index)
            if replaced_spool_id is not None and replaced_spool_id != spool_id:
                self._slot_to_filaman_spool.pop(slot_index, None)
                await self._restore_spool_location(replaced_spool_id)

            await self._cache_original_location(spool_id)
            await self._set_moonraker_active_spool(
                spool_id, extruder=self._slot_extruder(slot)
            )
            await self._execute_assign_macro(
                spool_id=spool_id,
                ams_id=ams_id,
                tray_id=tray_id,
                slot_index=slot_index,
                filament_data=filament_data,
            )
            await self._mark_slot(slot_index, filament_data, spool_id=spool_id)
            await self._move_spool_to_slot_location(spool_id, slot_index)
            self._slot_to_filaman_spool[slot_index] = spool_id

    async def assign_pending_spool(
        self,
        spool_id: int,
        filament_data: dict,
        slot_index: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        if self._auto_assign_confirm == "off":
            logger.info(
                "Auto-assign disabled (auto_assign_confirm=off); ignoring spool %s "
                "for printer %s",
                spool_id,
                self.printer_id,
            )
            return

        if self._pending_timer and not self._pending_timer.done():
            self._pending_timer.cancel()

        target_slot: dict[str, Any] | None = None
        if slot_index:
            target_slot = self._find_slot(slot_index)
        if target_slot is None and self._slots:
            target_slot = self._slots[0]
        target_extruder = self._slot_extruder(target_slot) if target_slot else None
        filament_was_present = (
            bool(self._filament_present.get(target_extruder))
            if target_extruder is not None
            else False
        )

        self._pending = {
            "spool_id": spool_id,
            "filament_data": dict(filament_data or {}),
            "slot_index": slot_index,
            "started_at": _now_iso(),
            "filament_was_present": filament_was_present,
        }

        if self._auto_assign_confirm == "immediate":
            await self._complete_pending_assignment()
            return

        # In sensor mode a real spool change (heat up, unload, insert) easily
        # exceeds the device-level auto-assign timeout, so the driver uses its
        # own window while waiting for the filament sensor.
        timeout = self._sensor_timeout
        if filament_was_present:
            logger.info(
                "Pending spool %s armed for printer %s while filament is already "
                "present on '%s' — likely a control weighing; assignment completes "
                "only after unload + reinsert within %s s",
                spool_id,
                self.printer_id,
                target_extruder,
                timeout,
            )
        else:
            logger.info(
                "Pending spool %s armed for printer %s — waiting for filament "
                "insertion on '%s' (timeout %s s)",
                spool_id,
                self.printer_id,
                target_extruder or "toolhead",
                timeout,
            )
        self._pending_timer = asyncio.create_task(self._pending_timeout_task(timeout))

    def _clear_pending(self) -> None:
        if self._pending_timer and not self._pending_timer.done():
            self._pending_timer.cancel()
        self._pending_timer = None
        self._pending = None

    async def _complete_pending_assignment(
        self, extruder: str | None = None, slot_index: str | None = None
    ) -> None:
        pending = self._pending
        if pending is None:
            return

        spool_id = pending.get("spool_id")
        if isinstance(spool_id, bool) or not isinstance(spool_id, int):
            self._clear_pending()
            return

        slot: dict[str, Any] | None = None
        # An explicit slot_index (e.g. from the box slot sensor) wins over the
        # pending's own hint, which is usually None for a scale weighing.
        resolved_index = slot_index or pending.get("slot_index")
        if resolved_index:
            slot = self._find_slot(resolved_index)
        if slot is None and extruder is not None:
            for candidate in self._slots:
                if self._slot_extruder(candidate) == extruder:
                    slot = candidate
                    break
        if slot is None and self._slots:
            slot = self._slots[0]
        if slot is None:
            logger.info(
                "Pending spool %s: no slot available to complete auto-assign "
                "for printer %s",
                spool_id,
                self.printer_id,
            )
            self._clear_pending()
            return

        ams_id, tray_id = self._slot_to_ids(slot.get("slot_index", "0-0"))
        filament_data = pending.get("filament_data") or {}
        self._clear_pending()
        try:
            await self.send_filament_to_tray(
                ams_id, tray_id, filament_data, spool_id=spool_id
            )
            logger.info(
                "Auto-assign completed for spool %s on slot %s-%s (printer %s)",
                spool_id,
                ams_id,
                tray_id,
                self.printer_id,
            )
        except Exception:
            logger.exception(
                "Auto-assign completion failed for spool %s (printer %s)",
                spool_id,
                self.printer_id,
            )

    async def _pending_timeout_task(self, timeout_seconds: int) -> None:
        await asyncio.sleep(timeout_seconds)
        if self._pending is not None:
            if self._pending.get("filament_was_present"):
                logger.info(
                    "Pending spool %s expired for printer %s without an unload — "
                    "treated as a control weighing, active spool unchanged",
                    self._pending.get("spool_id"),
                    self.printer_id,
                )
            else:
                logger.info(
                    "Pending spool %s timed out for printer %s",
                    self._pending.get("spool_id"),
                    self.printer_id,
                )
            self._pending = None

    def health(self) -> dict[str, Any]:
        return {
            "driver_key": self.driver_key,
            "printer_id": self.printer_id,
            "running": self._running,
            "connected": self._connected,
            "moonraker_url": self._moonraker_url,
            "mode": self._mode,
            "active_spool_id": self._active_spool_id,
            "pending": self._pending is not None,
            "pending_spool_id": self._pending.get("spool_id") if self._pending else None,
            "pending_started_at": (
                self._pending.get("started_at") if self._pending else None
            ),
            "pending_requires_unload": (
                bool(self._pending.get("filament_was_present"))
                if self._pending
                else None
            ),
            "auto_assign_confirm": self._auto_assign_confirm,
            "sensor_timeout_seconds": self._sensor_timeout,
            "filament_present": self._filament_present,
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
            "slot_count": len(self._slots),
            "printer_name": self._printer_name,
            "slots": self._slots,
            "ams_info": self._build_ams_info(),
            "tracked_slot_spools": self._slot_to_filaman_spool,
        }
