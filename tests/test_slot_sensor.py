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
    "_sensor_key_to_slot_index",
    "_read_slot_sensor",
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

body = "\n\n".join("    " + c.replace("\n", "\n    ") for c in chunks)
ns: dict[str, Any] = {"re": re, "Any": Any, "quote": lambda s, safe="": s}
exec(f"class Probe:\n{body}\n", ns)
Probe = ns["Probe"]


class Fake(Probe):
    def __init__(self, objects, path, per_slot, status, slots=None):
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
    (
        "Happy Hare (list, -1 unknown)",
        ["mmu"], "gate_status", False,
        {"mmu": {"gate_status": [-1, 0, 1, 2]}},
        {"0": False, "1": False, "2": True, "3": True},
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

for value, expected in VALUE_CASES:
    got = Probe._slot_sensor_present_value(value)
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] value {value!r} -> {got!r}"
          + ("" if ok else f" (expected {expected!r})"))

print()
if failures:
    print(f"{failures} FAILED")
    sys.exit(1)
print("all passed")
