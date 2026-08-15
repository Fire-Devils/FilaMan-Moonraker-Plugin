#!/usr/bin/env python3
"""Test slot-sensor parsing and slot resolution against the real firmware shapes.

The methods under test are EXTRACTED FROM driver.py itself (ast, by name) rather
than retyped here — a retyped copy would test the copy, not the shipped code.
Only the HTTP transport is faked; no network, no printer.

Run: python3 tests/test_slot_sensor.py [path/to/driver.py]

Payload shapes:
  QIDI BOX    tests/fixtures/qidi_multi_color_controller.json — captured verbatim
              from a live printer, so the 0/1/2 polarity is evidence rather than
              a claim. Note it pads to slot15 no matter how many boxes exist.
  Happy Hare  gate_status is a list; constants GATE_UNKNOWN/-1, GATE_EMPTY/0,
              GATE_AVAILABLE/1, GATE_AVAILABLE_FROM_BUFFER/2 in extras/mmu/mmu.py.
  AFC         one object per lane; AFCLane.get_status() sets
              response["prep"] = bool(self.prep_state) in extras/AFC_lane.py.

Third-party line numbers are deliberately not cited: they rot silently. Symbols
are stable enough to search for.
"""
import ast
import asyncio
import json
import os
import re
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.path.join(HERE, "..", "moonraker_filaman", "driver.py")
)
WANTED = {
    "_slot_sensor_present_value",
    "_slot_sensor_value_unknown",
    "_sensor_key_to_slot_index",
    "_read_slot_sensor",
    "_sensor_edges",
    "_sensor_baseline_from_slots",
    "_release_blockers",
    "_handle_vacated_slots",
    "_find_slot",
    "_slot_to_ids",
    "_dig",
}

source = open(PATH, encoding="utf-8").read()
tree = ast.parse(source)

cls = next(
    node
    for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name == "Driver"
)

chunks = []
found = set()
for node in cls.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED:
        # get_source_segment starts at `def`, dropping decorators — @staticmethod
        # would be lost and `self` would silently become the first argument.
        decorators = "".join(
            f"@{ast.get_source_segment(source, d)}\n" for d in node.decorator_list
        )
        chunks.append(decorators + ast.get_source_segment(source, node))
        found.add(node.name)

missing = WANTED - found
if missing:
    print(f"FAIL: methods not found in {PATH}: {sorted(missing)}")
    sys.exit(1)

def module_const(name):
    """Read a module-level constant out of driver.py rather than retyping it."""
    for n in tree.body:
        # AnnAssign for an annotated constant, Assign for a bare one.
        targets = [n.target] if isinstance(n, ast.AnnAssign) else getattr(n, "targets", [])
        if targets and getattr(targets[0], "id", "") == name:
            return n
    print(f"FAIL: constant {name} not found in {PATH}")
    sys.exit(1)


RELEASE_CONFIRM_POLLS = ast.literal_eval(module_const("RELEASE_CONFIRM_POLLS").value)

# The QIDI preset carries the conditions a release is gated on. Taken from the
# source, not retyped: a test with its own copy would keep passing after the
# shipped values changed.
QIDI_PRESET = None
for element in module_const("SLOT_SENSOR_PRESETS").value.elts:
    try:
        preset = ast.literal_eval(element)
    except ValueError:
        continue  # the AFC entry references a name, not a literal
    if preset.get("match") == "multi_color_controller":
        QIDI_PRESET = preset
if QIDI_PRESET is None or not QIDI_PRESET.get("release_when"):
    print("FAIL: the QIDI preset carries no release_when")
    sys.exit(1)

body = "\n\n".join("    " + c.replace("\n", "\n    ") for c in chunks)
ns: dict[str, Any] = {
    "re": re,
    "Any": Any,
    "quote": lambda s, safe="": s,
    "RELEASE_CONFIRM_POLLS": RELEASE_CONFIRM_POLLS,
}
exec(f"class Probe:\n{body}\n", ns)
Probe = ns["Probe"]


class Fake(Probe):
    def __init__(self, objects, path, per_slot, status, slots=None,
                 health=None, printer_state=None, release_when=None):
        self.printer_id = 1
        self._slot_sensor_objects = objects
        self._slot_sensor_states_path = path
        self._slot_sensor_per_slot = per_slot
        self._slot_sensor_labels = {}
        self._status = status
        self._warned = []
        self._slots = slots if slots is not None else [
            {"slot_index": f"1-{i}", "slot_kind": "tray"} for i in range(4)
        ]
        self._pending_release = {}
        self._slot_sensor_health_obj = health
        self._printer_state = printer_state
        self._slot_sensor_release_when = release_when or {}
        self._released = []

    async def _release_slot(self, key, slot_index, spool_id):
        self._released.append((key, slot_index, spool_id))
        slot = self._find_slot(slot_index)
        if slot is not None:
            slot["spool_id"] = None
            slot["present"] = False

    async def _request(self, method, path):
        return {"result": {"status": self._status}}

    @staticmethod
    def _unwrap_result(payload):
        return payload.get("result", payload)

    def _warn_once(self, key, message, *args):
        self._warned.append(key)

    def _clear_warn_once(self, key):
        pass


QIDI = json.load(open(os.path.join(HERE, "fixtures", "qidi_multi_color_controller.json")))
QIDI_STATUS = QIDI["result"]["status"]
QIDI_EXPECTED = {str(i): (i < 4) for i in range(16)}

TRAYS_DISCOVERED = [{"slot_index": f"1-{i}", "slot_kind": "tray"} for i in range(4)]
TRAYS_CONFIGURED = [{"slot_index": f"0-{i}", "slot_kind": "tray"} for i in range(4)]
TOOLHEAD_ONLY = [{"slot_index": "0-0", "slot_kind": "toolhead"}]

READ_CASES = [
    (
        "QIDI BOX (live fixture; dict, 0/1/2, padded to slot15)",
        ["multi_color_controller"], "slots.states", False, QIDI_STATUS,
        QIDI_EXPECTED,
    ),
    # GATE_UNKNOWN (-1) is DROPPED, not reported as empty: as a baseline it
    # would make the next real reading look like an insertion, and as a current
    # value it would look like a removal and unbind the spool.
    (
        "Happy Hare (list, -1 unknown is not a reading)",
        ["mmu"], "gate_status", False,
        {"mmu": {"gate_status": [-1, 0, 1, 2]}},
        {"1": False, "2": True, "3": True},
    ),
    (
        "AFC (object per lane; keyed by POSITION, not by lane number)",
        ["AFC_stepper lane1", "AFC_stepper lane2"], "prep", True,
        {
            "AFC_stepper lane1": {"prep": True, "lane": 1},
            "AFC_stepper lane2": {"prep": False, "lane": 2},
        },
        {"0": True, "1": False},
    ),
    (
        "wrong path -> nothing usable, not a false 'all empty'",
        ["multi_color_controller"], "slots.nope", False, QIDI_STATUS,
        {},
    ),
    (
        "no path configured -> refuses to guess",
        ["multi_color_controller"], "", False, QIDI_STATUS,
        {},
    ),
]

# (slot list, canonical slot number, expected slot_index)
KEY_CASES = [
    ("discovered trays", TRAYS_DISCOVERED, "3", "1-3"),
    ("discovered trays", TRAYS_DISCOVERED, "0", "1-0"),
    ("configured trays", TRAYS_CONFIGURED, "2", "0-2"),
    # QIDI pads its state map well past the physical slots; those must resolve to
    # nothing rather than to a fabricated index.
    ("padded slot beyond the trays", TRAYS_DISCOVERED, "7", None),
    # The regression this whole change exists for: with no tray slots the old
    # code fabricated "0-N", which then resolved to the TOOLHEAD.
    ("toolhead-only printer", TOOLHEAD_ONLY, "2", None),
    ("non-numeric", TRAYS_DISCOVERED, "lane", None),
]

VALUE_CASES = [
    (True, True), (False, False),
    (-1, False),          # Happy Hare GATE_UNKNOWN must NOT read as present
    (0, False), (1, True), (2, True),
    ("loaded", None), (None, None), ({}, None),
]

failures = 0

for name, objects, path, per_slot, status, expected in READ_CASES:
    got = asyncio.run(Fake(objects, path, per_slot, status)._read_slot_sensor())
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] read: {name}")
    if not ok:
        print(f"        expected {expected}\n        got      {got}")

for label, slots, key, expected in KEY_CASES:
    probe = Fake([], "", False, {}, slots=slots)
    got = probe._sensor_key_to_slot_index(key)
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] slot {key!r} on {label} -> {got!r}"
          + ("" if ok else f" (expected {expected!r})"))

# (label, previous, current, expected appeared, expected vacated)
EDGE_CASES = [
    ("insertion", {"1": False}, {"1": True}, ["1"], []),
    ("removal", {"1": True}, {"1": False}, [], ["1"]),
    # Both directions need a baseline: the first poll after a start knows
    # nothing, and guessing either way writes a wrong assignment.
    ("first poll, occupied", {}, {"1": True}, [], []),
    ("first poll, empty", {}, {"1": False}, [], []),
    ("steady occupied", {"1": True}, {"1": True}, [], []),
    ("steady empty", {"1": False}, {"1": False}, [], []),
    (
        "swap in one poll",
        {"0": True, "1": False},
        {"0": False, "1": True},
        ["1"],
        ["0"],
    ),
    # The AMS dropping off the bus: every occupied slot reads empty at once.
    # _release_vacated_slots() refuses this shape; the edges themselves are real.
    (
        "everything gone at once",
        {"0": True, "1": True, "2": True},
        {"0": False, "1": False, "2": False},
        [],
        ["0", "1", "2"],
    ),
]

# (label, slots, expected baseline)
BASELINE_CASES = [
    (
        "assigned trays only",
        [
            {"slot_index": "0-0", "slot_kind": "tray", "spool_id": 66},
            {"slot_index": "0-1", "slot_kind": "tray", "spool_id": None},
            {"slot_index": "0-2", "slot_kind": "tray", "spool_id": 92},
        ],
        {"0": True, "2": True},
    ),
    # Toolhead slots have no slot sensor; seeding them would fabricate an edge
    # against a key _read_slot_sensor() never produces.
    (
        "toolhead ignored",
        [
            {"slot_index": "0-0", "slot_kind": "toolhead", "spool_id": 31},
            {"slot_index": "0-1", "slot_kind": "tray", "spool_id": 7},
        ],
        {"1": True},
    ),
    ("nothing assigned", [{"slot_index": "0-0", "slot_kind": "tray"}], {}),
    # _slot_to_ids() answers (0, 0) for anything it cannot parse, and
    # slot_targets accepts any non-empty slot_index — so a hand-written or
    # mistyped one would seed the baseline of slot 0, a different real slot.
    (
        "unparsable slot_index seeds nothing",
        [
            {"slot_index": "toolhead", "slot_kind": "tray", "spool_id": 31},
            {"slot_index": "0-1", "slot_kind": "tray", "spool_id": 7},
        ],
        {"1": True},
    ),
    # Cascaded units share slot numbers ("1-0" and "2-0" are both slot 0) and
    # the sensor key cannot say which is meant; releasing the wrong unit's slot
    # is worse than not noticing the removal.
    (
        "colliding units seed neither",
        [
            {"slot_index": "1-0", "slot_kind": "tray", "spool_id": 11},
            {"slot_index": "2-0", "slot_kind": "tray", "spool_id": 77},
            {"slot_index": "1-1", "slot_kind": "tray", "spool_id": 5},
        ],
        {"1": True},
    ),
]

for label, previous, current, exp_appeared, exp_vacated in EDGE_CASES:
    appeared, vacated = Probe._sensor_edges(previous, current)
    got = (sorted(appeared), sorted(vacated))
    expected = (sorted(exp_appeared), sorted(exp_vacated))
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] edges: {label} -> {got}"
          + ("" if ok else f" (expected {expected})"))

for label, slots, expected in BASELINE_CASES:
    got = Fake([], "", False, {}, slots=slots)._sensor_baseline_from_slots()
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] baseline: {label} -> {got}"
          + ("" if ok else f" (expected {expected})"))

for value, expected in VALUE_CASES:
    got = Probe._slot_sensor_present_value(value)
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] value {value!r} -> {got!r}"
          + ("" if ok else f" (expected {expected!r})"))

for value, expected in [(-1, True), (-0.5, True), (0, False), (2, False),
                        (True, False), (False, False), ("x", False), (None, False)]:
    got = Probe._slot_sensor_value_unknown(value)
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] unknown? {value!r} -> {got!r}"
          + ("" if ok else f" (expected {expected!r})"))

# ---- release gating -------------------------------------------------------
# Live shapes from the printer, 2026-08-14. The box reports these next to the
# slot states in the same object, which is why gating costs no extra request.
IDLE_BOX = {
    "hardware": {"box_count": 1, "connected": True},
    "system": {"ready": True, "mode": "local"},
    "operation": {"current": -1, "progress": 0, "error": None},
    "print": {"printing": False, "current_tool": -1, "next_tool": -1},
}


def box(**overrides):
    out = json.loads(json.dumps(IDLE_BOX))
    for path, value in overrides.items():
        section, _, field = path.partition("__")
        out[section][field] = value
    return out


# (label, health object, print_stats.state, expected blocker count)
BLOCKER_CASES = [
    ("idle box, idle printer", IDLE_BOX, "standby", 0),
    # A tool change empties a slot mid-print, and a runout leaves the spool in.
    ("printing", IDLE_BOX, "printing", 1),
    ("paused", IDLE_BOX, "paused", 1),
    # The box working the filament itself: BOX_EXTRACT retracts it out of the
    # hub on command, which reads exactly like a removal.
    ("box busy", box(operation__current=2), "standby", 1),
    ("box printing flag", box(print__printing=True), "standby", 1),
    ("box off the bus", box(hardware__connected=False), "standby", 1),
    ("box not ready", box(system__ready=False), "standby", 1),
    ("two at once", box(hardware__connected=False), "printing", 2),
    # A firmware that publishes none of these fields must not be gated on them.
    ("fields absent", {}, "standby", 0),
]

for label, health, state, expected in BLOCKER_CASES:
    probe = Fake([], "", False, {}, health=health, printer_state=state,
                 release_when=QIDI_PRESET["release_when"])
    got = probe._release_blockers()
    ok = len(got) == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] blockers: {label} -> {got}"
          + ("" if ok else f" (expected {expected})"))


def releaser(slots, state="standby", health=None):
    return Fake([], "", False, {}, slots=slots, printer_state=state,
                health=health if health is not None else IDLE_BOX,
                release_when=QIDI_PRESET["release_when"])


def assigned(spool_id=92, number=2):
    return [{"slot_index": f"0-{number}", "slot_kind": "tray",
             "spool_id": spool_id, "present": True}]


def vacate(probe, times, key="2"):
    for _ in range(times):
        asyncio.run(probe._handle_vacated_slots([key]))


# One reading must not be enough: the release is destructive and re-inserting
# the spool does not undo it.
probe = releaser(assigned())
vacate(probe, RELEASE_CONFIRM_POLLS - 1)
early = list(probe._released)
vacate(probe, 1)
ok = not early and probe._released == [("2", "0-2", 92)]
failures += not ok
print(f"[{'ok ' if ok else 'FAIL'}] release: waits {RELEASE_CONFIRM_POLLS} polls "
      f"(after {RELEASE_CONFIRM_POLLS - 1}: {early}, then {probe._released})")

# Blocked for as long as the printer is busy, and NOT forgotten: the pending
# entry is what re-offers the removal once it clears.
probe = releaser(assigned(), state="printing")
vacate(probe, 10)
blocked = list(probe._released)
probe._printer_state = "standby"
vacate(probe, RELEASE_CONFIRM_POLLS)
ok = not blocked and probe._released == [("2", "0-2", 92)]
failures += not ok
print(f"[{'ok ' if ok else 'FAIL'}] release: held while printing, resolves after "
      f"(during: {blocked}, after: {probe._released})")

# The slot being reassigned under us restarts the evidence, so a spool that was
# just bound cannot be released on readings taken before it existed.
probe = releaser(assigned())
vacate(probe, RELEASE_CONFIRM_POLLS - 1)
probe._slots[0]["spool_id"] = 77
vacate(probe, 1)
mid = list(probe._released)
vacate(probe, RELEASE_CONFIRM_POLLS - 1)
ok = not mid and probe._released == [("2", "0-2", 77)]
failures += not ok
print(f"[{'ok ' if ok else 'FAIL'}] release: reassignment restarts the count "
      f"(after swap: {mid}, then {probe._released})")

# QIDI pads its state map to 16 slots whatever the box holds; those keys resolve
# to no tray and must not accumulate anything.
probe = releaser(assigned())
vacate(probe, RELEASE_CONFIRM_POLLS + 2, key="9")
ok = not probe._released and not probe._pending_release
failures += not ok
print(f"[{'ok ' if ok else 'FAIL'}] release: padding slot ignored "
      f"({probe._released}, pending={probe._pending_release})")

# An empty slot with nothing bound is not a removal - and its stale `present`
# gets corrected rather than carried forward.
probe = releaser([{"slot_index": "0-2", "slot_kind": "tray",
                   "spool_id": None, "present": True}])
vacate(probe, RELEASE_CONFIRM_POLLS + 1)
ok = (not probe._released and not probe._pending_release
      and probe._slots[0]["present"] is False)
failures += not ok
print(f"[{'ok ' if ok else 'FAIL'}] release: unbound slot only clears present "
      f"({probe._released}, present={probe._slots[0]['present']})")

print()
if failures:
    print(f"{failures} FAILED")
    sys.exit(1)
print("all passed")
