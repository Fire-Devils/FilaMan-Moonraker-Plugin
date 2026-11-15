"""Run with the FilaMan backend on PYTHONPATH."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1.spools import _reject_driver_managed_create_location
from app.models import Base, Filament, Location, Manufacturer, Printer, Spool, SpoolStatus
from moonraker_filaman import driver as moonraker


class SlotLocationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(self.engine.dispose)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        session_patch = patch.object(moonraker, "async_session_maker", self.sessions)
        session_patch.start()
        self.addCleanup(session_patch.stop)

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as db:
            manufacturer = Manufacturer(name="Test")
            status = SpoolStatus(key="active", label="Active")
            self.printer = Printer(name="Moonraker", driver_key="moonraker_filaman")
            self.shelf = Location(name="Shelf")
            db.add_all([manufacturer, status, self.printer, self.shelf])
            await db.flush()
            filament = Filament(manufacturer_id=manufacturer.id, designation="PLA", material_type="PLA", diameter_mm=1.75)
            db.add(filament)
            await db.flush()
            self.spool = Spool(filament_id=filament.id, status_id=status.id, location_id=self.shelf.id)
            db.add(self.spool)
            await db.commit()

    def driver(self):
        driver = moonraker.Driver(
            self.printer.id,
            {"moonraker_url": "http://printer.test", "slot_count": 2},
            lambda _event: None,
        )
        driver._request = AsyncMock(return_value={"result": {}})
        return driver

    async def assert_protected(self, db, location_id):
        with self.assertRaises(HTTPException) as result:
            await _reject_driver_managed_create_location(db, {"location_id": location_id})
        self.assertEqual(result.exception.detail["code"], "driver_managed_location")

    async def test_assignment_protects_the_slot_and_release_restores_storage(self):
        driver = self.driver()
        await driver.send_filament_to_tray(0, 0, {"id": self.spool.id, "material_type": "PLA"})
        self.assertEqual(driver._request.await_args.args[:2], ("POST", "/server/filaman/spool_id"))
        self.assertEqual(driver._request.await_args.args[2]["spool_id"], self.spool.id)
        async with self.sessions() as db:
            spool = await db.get(Spool, self.spool.id)
            slot = await db.get(Location, spool.location_id)
            self.assertEqual(slot.identifier, "moonraker_1_0-0")
            await self.assert_protected(db, slot.id)

        await driver._release_slot("0", "0-0", self.spool.id)
        async with self.sessions() as db:
            spool = await db.get(Spool, self.spool.id)
            self.assertEqual(spool.location_id, self.shelf.id)

    async def test_restart_repairs_only_owned_legacy_slots_including_empty_slots(self):
        def legacy(identifier, printer_id, slot_index):
            return Location(
                name=identifier,
                identifier=identifier,
                custom_fields={
                    "managed_by": "moonraker_filaman",
                    "printer_id": printer_id,
                    "slot_index": slot_index,
                    "note": "keep",
                },
            )

        occupied = legacy("moonraker_1_0-0", self.printer.id, "0-0")
        empty = legacy("moonraker_1_0-1", self.printer.id, "0-1")
        other = legacy("moonraker_10_0-0", 10, "0-0")
        wrong_owner = legacy("moonraker_1_0-2", 10, "0-2")
        unrelated = legacy("storage_1_0-0", self.printer.id, "0-0")
        malformed = legacy("moonraker_1_0-3", True, "0-3")
        async with self.sessions() as db:
            db.add_all([occupied, empty, other, wrong_owner, unrelated, malformed])
            await db.flush()
            (await db.get(Spool, self.spool.id)).location_id = occupied.id
            await db.commit()

        for _ in range(2):
            driver = self.driver()
            try:
                await driver.start()
                self.assertEqual(driver._find_slot("0-0")["spool_id"], self.spool.id)
                self.assertFalse(driver._find_slot("0-1")["present"])
            finally:
                await driver.stop()

            async with self.sessions() as db:
                for slot in (occupied, empty):
                    repaired = await db.get(Location, slot.id)
                    self.assertEqual(repaired.custom_fields, {**slot.custom_fields, "managed_by": "moonraker_filaman_plugin"})
                    await self.assert_protected(db, slot.id)
                for location in (other, wrong_owner, unrelated, malformed):
                    self.assertEqual((await db.get(Location, location.id)).custom_fields, location.custom_fields)
                self.assertEqual((await db.get(Spool, self.spool.id)).location_id, occupied.id)

    async def test_sqlite_migration_preserves_a_competing_location_edit(self):
        with TemporaryDirectory() as directory:
            engine = create_async_engine(
                f"sqlite+aiosqlite:///{Path(directory) / 'filaman.sqlite'}",
                connect_args={"timeout": 0.05},
            )
            try:
                async with engine.begin() as connection:
                    await connection.execute(text("PRAGMA journal_mode=WAL"))
                    await connection.run_sync(Base.metadata.create_all)
                sessions = async_sessionmaker(engine, expire_on_commit=False)
                async with sessions() as db:
                    slot = Location(
                        name="Tray",
                        identifier="moonraker_1_0-0",
                        custom_fields={
                            "managed_by": "moonraker_filaman",
                            "printer_id": self.printer.id,
                            "slot_index": "0-0",
                            "note": "before edit",
                        },
                    )
                    db.add(slot)
                    await db.commit()

                async def edit_location():
                    async with sessions() as db:
                        row = await db.get(Location, slot.id)
                        row.custom_fields = {**row.custom_fields, "note": "after edit"}
                        await db.commit()

                class SessionWithCompetingEdit(AsyncSession):
                    async def scalars(self, *args, **kwargs):
                        rows = await super().scalars(*args, **kwargs)
                        try:
                            await edit_location()
                        except OperationalError as exc:
                            if "database is locked" not in str(exc):
                                raise
                            self.edit_waiting = True
                        return rows

                async with SessionWithCompetingEdit(engine) as migration:
                    migration.edit_waiting = False
                    with patch.object(moonraker, "async_session_maker", return_value=migration):
                        await self.driver()._mark_legacy_slot_locations()
                    if migration.edit_waiting:
                        await edit_location()

                async with sessions() as db:
                    row = await db.get(Location, slot.id)
                    self.assertEqual(row.custom_fields["note"], "after edit")
                    await self.assert_protected(db, slot.id)
            finally:
                await engine.dispose()

    async def test_ordinary_location_with_slot_identifier_stays_storage(self):
        async with self.sessions() as db:
            shelf = await db.get(Location, self.shelf.id)
            shelf.identifier = "moonraker_1_0-0"
            shelf.custom_fields = {"note": "ordinary shelf"}
            alternate_shelf = Location(name="Other shelf", identifier=" MOONRAKER_1_0-0-2 ")
            db.add(alternate_shelf)
            parked = Spool(
                filament_id=self.spool.filament_id,
                status_id=self.spool.status_id,
                location_id=shelf.id,
            )
            db.add(parked)
            await db.commit()

        driver = self.driver()
        await driver.send_filament_to_tray(0, 0, {"id": self.spool.id, "material_type": "PLA"})
        async with self.sessions() as db:
            moved = await db.get(Spool, self.spool.id)
            self.assertNotEqual(moved.location_id, self.shelf.id)
            await self.assert_protected(db, moved.location_id)
            slot = await db.get(Location, moved.location_id)
            shelf = await db.get(Location, self.shelf.id)
            self.assertEqual(slot.identifier, "moonraker_1_0-0-3")
            self.assertEqual(shelf.custom_fields, {"note": "ordinary shelf"})
            self.assertEqual((await db.get(Location, alternate_shelf.id)).name, "Other shelf")
            await _reject_driver_managed_create_location(db, {"location_id": shelf.id})

        restarted = self.driver()
        try:
            await restarted.start()
            self.assertEqual(restarted._find_slot("0-0")["spool_id"], self.spool.id)
        finally:
            await restarted.stop()
        await restarted._clear_slot_location(parked.id, "0-0")
        async with self.sessions() as db:
            self.assertEqual((await db.get(Spool, parked.id)).location_id, self.shelf.id)

    async def test_restart_ignores_a_newer_archived_spool_at_an_owned_slot(self):
        driver = self.driver()
        await driver.send_filament_to_tray(0, 0, {"id": self.spool.id, "material_type": "PLA"})
        async with self.sessions() as db:
            archived_status = SpoolStatus(key="archived", label="Archived")
            db.add(archived_status)
            await db.flush()
            assigned = await db.get(Spool, self.spool.id)
            db.add(
                Spool(
                    filament_id=assigned.filament_id,
                    status_id=archived_status.id,
                    location_id=assigned.location_id,
                )
            )
            await db.commit()

        restarted = self.driver()
        try:
            await restarted.start()
            self.assertEqual(restarted._find_slot("0-0")["spool_id"], self.spool.id)
        finally:
            await restarted.stop()
