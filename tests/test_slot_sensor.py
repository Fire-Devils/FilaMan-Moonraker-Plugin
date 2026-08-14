#!/usr/bin/env python3
"""Test slot-sensor parsing against the three real firmware shapes.

The methods under test are EXTRACTED FROM driver.py itself (ast, by name) rather
than retyped here — a retyped copy would test the copy, not the shipped code.
Only the HTTP transport is faked.

Payload shapes are taken from the vendors' sources:
  QIDI BOX    live capture from the printer, multi_color_controller.slots.states
  Happy Hare  extras/mmu/mmu.py:59-62  (-1 unknown, 0 empty, 1/2 available)
              'gate_status': self.gate_status   -> list
  AFC         extras/AFC_lane.py:2195  response["prep"] = bool(self.prep_state)
"""
import ast
import os
import asyncio
import re
import sys
from typing import Any

PATH = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.path.join(os.path.dirname(__file__), "..", "moonraker_filaman", "driver.py")
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
    if isinstance(node, ast.ClassDef) and any(
        isinstance(b, ast.FunctionDef) or isinstance(b, ast.AsyncFunctionDef)
        for b in node.body
    )
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

# Rebuild a minimal class carrying the real method bodies. Decorators come along
# with get_source_segment, so @staticmethod survives.
body = "\n\n".join("    " + c.replace("\n", "\n    ") for c in chunks)
ns: dict[str, Any] = {"re": re, "Any": Any, "logger": None}
exec(f"class Probe:\n{body}\n", ns)
Probe = ns["Probe"]


class Fake(Probe):
    def __init__(self, objects, path, per_slot, status):
        self._slot_sensor_objects = objects
        self._slot_sensor_states_path = path
        self._slot_sensor_per_slot = per_slot
        self._status = status
        self._slots = [
            {"slot_index": f"0-{i}", "slot_kind": "tray"} for i in range(4)
        ]

    async def _request(self, method, path):
        return {"result": {"status": self._status}}

    @staticmethod
    def _unwrap_result(payload):
        return payload.get("result", payload)


CASES = [
    (
        "QIDI BOX (dict, 0/1/2)",
        ["multi_color_controller"],
        "slots.states",
        False,
        {"multi_color_controller": {"slots": {"states": {
            "slot0": 2, "slot1": 1, "slot2": 0, "slot3": 0}}}},
        {"slot0": True, "slot1": True, "slot2": False, "slot3": False},
    ),
    (
        "Happy Hare (list, -1 unknown)",
        ["mmu"],
        "gate_status",
        False,
        {"mmu": {"gate_status": [-1, 0, 1, 2]}},
        {"0": False, "1": False, "2": True, "3": True},
    ),
    (
        "AFC (one object per lane, bool prep)",
        ["AFC_stepper lane1", "AFC_stepper lane2"],
        "prep",
        True,
        {
            "AFC_stepper lane1": {"prep": True, "lane": 1},
            "AFC_stepper lane2": {"prep": False, "lane": 2},
        },
        {"AFC_stepper lane1": True, "AFC_stepper lane2": False},
    ),
]

KEY_CASES = [
    ("slot3", "0-3"),
    ("2", "0-2"),
    ("AFC_stepper lane1", "0-1"),
    ("gate0", "0-0"),
    ("nonsense", None),
]

VALUE_CASES = [
    (True, True), (False, False),
    (-1, False),          # Happy Hare GATE_UNKNOWN must NOT read as present
    (0, False), (1, True), (2, True),
    ("weird", None), (None, None),
]

failures = 0

for name, objects, path, per_slot, status, expected in CASES:
    got = asyncio.run(Fake(objects, path, per_slot, status)._read_slot_sensor())
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        expected {expected}\n        got      {got}")

probe = Fake([], "", False, {})
for key, expected in KEY_CASES:
    got = probe._sensor_key_to_slot_index(key)
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] key {key!r} -> {got!r}" + ("" if ok else f" (expected {expected!r})"))

for value, expected in VALUE_CASES:
    got = Probe._slot_sensor_present_value(value)
    ok = got == expected
    failures += not ok
    print(f"[{'ok ' if ok else 'FAIL'}] value {value!r} -> {got!r}" + ("" if ok else f" (expected {expected!r})"))

print()
if failures:
    print(f"{failures} FAILED")
    sys.exit(1)
print("all passed")
