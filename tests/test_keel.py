# -*- coding: utf-8 -*-
"""pytest wrapper around the layer's self test, plus a few pointed schema and metric checks."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from keel import extract, schema, vitals  # noqa: E402
from keel.selftest import run  # noqa: E402


def test_selftest_both_ends():
    assert run() == 0


def test_schema_requires_known_type():
    with pytest.raises(schema.SchemaError):
        schema.make_event("nope", session="s", project="p")


def test_schema_ok():
    e = schema.make_event("claim", session="s", project="p", data={"kind": "done"})
    assert e["ev"] == "claim" and e["agent"] == "head"


def test_edit_payload_is_not_context_flood():
    payload = {"filePath": "a.py", "originalFile": "x" * 50_000, "oldString": "a", "newString": "b", "structuredPatch": []}
    assert extract.tool_result_len("Edit", payload) == extract.EDIT_CONFIRM_CHARS
    assert extract.content_len(payload) > 20_000  # what the old ruler would have counted


def test_image_is_a_fixed_estimate():
    image = {"type": "image", "file": {"base64": "A" * 500_000}}
    assert extract.tool_result_len("Read", image) == extract.IMAGE_TOKENS * extract.CHARS_PER_TOKEN


def test_active_time_ignores_idle_gaps():
    def ev(t):
        return schema.make_event("tool.call", session="s", project="p", ts=t,
                                 data={"tool": "Bash", "ok": True, "ms": 1, "in_bytes": 1, "out_bytes": 1, "sig": "x"})
    stamps = ["2026-09-02T10:00:00+00:00", "2026-09-02T10:05:00+00:00",   # 5 min of work
              "2026-09-02T12:00:00+00:00", "2026-09-02T12:05:00+00:00"]   # 2 h idle gap, then 5 more minutes
    assert vitals.active_seconds([ev(t) for t in stamps]) == 600.0
