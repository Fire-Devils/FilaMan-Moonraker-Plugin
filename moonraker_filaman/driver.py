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

        self._slots: list[dict[str, Any]] = self._build_initial_slots()
        self._active_spool_id: int | None = None
        self._pending: dict[str, Any] | None = None
        self._pending_timer: asyncio.Task | None = None
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
            headers["X-Api-Key"] = self._api_key
            headers["Authorization"] = f"Bearer {self._api_key}"
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

    async def _set_moonraker_active_spool(self, spool_id: int | None) -> None:
        await self._request(
            "POST",
            "/server/filaman/spool_id",
            {"spool_id": spool_id},
        )
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
    ) -> None:
        tray_type = str(filament_data.get("material_type") or "PLA")
        tray_color = str(filament_data.get("color") or "FFFFFF").replace("#", "")[:6]
        tray_info_idx = str(filament_data.get("tray_info_idx") or "FILAMAN")

        changed = False
        for slot in self._slots:
            if slot.get("slot_index") != slot_index:
                continue
            slot.update(
                {
                    "tray_type": tray_type,
                    "tray_color": tray_color,
                    "tray_info_idx": tray_info_idx,
                    "present": True,
                }
            )
            changed = True
            break

        if not changed:
            self._slots.append(
                {
                    "slot_index": slot_index,
                    "slot_name": slot_index,
                    "tray_info_idx": tray_info_idx,
                    "tray_type": tray_type,
                    "tray_color": tray_color,
                    "present": True,
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

    def _active_slot_index(self) -> str:
        for slot in self._slots:
            if str(slot.get("slot_kind") or "").lower() == "toolhead":
                return str(slot.get("slot_index") or "0-0")

        for slot in self._slots:
            if str(slot.get("slot_index") or "") == "0-0":
                return "0-0"

        if self._slots:
            return str(self._slots[0].get("slot_index") or "0-0")

        return "0-0"

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
        try:
            payload = await self._request("GET", "/server/filaman/status")
            result = self._unwrap_result(payload)
            self._connected = bool(result.get("filaman_connected", True))
            self._active_spool_id = result.get("spool_id")
            self._last_error = None
            self._last_success_at = _now_iso()
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)

        await self._refresh_slots(emit=False)

        if self._active_spool_id is not None:
            target_slot_index = self._active_slot_index()
            await self._cache_original_location(self._active_spool_id)
            await self._move_spool_to_slot_location(
                self._active_spool_id,
                target_slot_index,
            )
            self._slot_to_filaman_spool[target_slot_index] = self._active_spool_id

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

        if previous_spool_id != self._active_spool_id:
            async with self._lock:
                if previous_spool_id is not None:
                    previous_slot_index = self._find_slot_for_spool(previous_spool_id)
                    if previous_slot_index:
                        self._slot_to_filaman_spool.pop(previous_slot_index, None)
                    await self._restore_spool_location(previous_spool_id)

                if self._active_spool_id is not None:
                    target_slot_index = self._active_slot_index()
                    old_slot_for_active = self._find_slot_for_spool(self._active_spool_id)
                    if old_slot_for_active and old_slot_for_active != target_slot_index:
                        self._slot_to_filaman_spool.pop(old_slot_for_active, None)

                    replaced_spool_id = self._slot_to_filaman_spool.get(target_slot_index)
                    if (
                        replaced_spool_id is not None
                        and replaced_spool_id != self._active_spool_id
                    ):
                        self._slot_to_filaman_spool.pop(target_slot_index, None)
                        await self._restore_spool_location(replaced_spool_id)

                    await self._cache_original_location(self._active_spool_id)
                    await self._move_spool_to_slot_location(
                        self._active_spool_id,
                        target_slot_index,
                    )
                    self._slot_to_filaman_spool[target_slot_index] = self._active_spool_id

            self._emit_slots_update()

        return {
            "connected": self._connected,
            "active_spool_id": self._active_spool_id,
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
        slot = next(
            (item for item in self._slots if item.get("slot_index") == slot_index),
            None,
        )
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
            await self._set_moonraker_active_spool(spool_id)
            await self._execute_assign_macro(
                spool_id=spool_id,
                ams_id=ams_id,
                tray_id=tray_id,
                slot_index=slot_index,
                filament_data=filament_data,
            )
            await self._mark_slot(slot_index, filament_data)
            await self._move_spool_to_slot_location(spool_id, slot_index)
            self._slot_to_filaman_spool[slot_index] = spool_id

    async def assign_pending_spool(
        self,
        spool_id: int,
        filament_data: dict,
        slot_index: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        if self._pending_timer and not self._pending_timer.done():
            self._pending_timer.cancel()

        self._pending = {
            "spool_id": spool_id,
            "filament_data": dict(filament_data or {}),
            "slot_index": slot_index,
            "started_at": _now_iso(),
        }

        timeout = timeout_seconds if timeout_seconds is not None else 60
        self._pending_timer = asyncio.create_task(self._pending_timeout_task(timeout))

    async def _pending_timeout_task(self, timeout_seconds: int) -> None:
        await asyncio.sleep(timeout_seconds)
        if self._pending is not None:
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
            "last_error": self._last_error,
            "last_success_at": self._last_success_at,
            "slot_count": len(self._slots),
            "printer_name": self._printer_name,
            "slots": self._slots,
            "ams_info": self._build_ams_info(),
            "tracked_slot_spools": self._slot_to_filaman_spool,
        }
