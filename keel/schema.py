# -*- coding: utf-8 -*-
"""Ledger event schema. One event = one JSON line.

Field names are aligned with the OpenTelemetry GenAI semantic conventions where that makes
sense (gen_ai.usage.input_tokens and the like in the usage data), so the ledger can be
exported to any tracer without renaming.

Required fields of every event:
    ts       ISO-8601 with zone: when the event happened (not when it was written)
    ev       event type, one of EVENT_TYPES
    session  agent session identifier
    project  project name (launch directory or an explicit name)
    agent    "head" for the main session, "sub" or "sub:<id>" for a subagent
    src      where it came from: hook | backfill | cli | mcp | test | tg | owner
    data     payload dictionary, fields depend on the type

Optional:
    task     identifier of the task (contract) the event belongs to
    seq      sequence number inside the session, if known
    prev, h  hash chain, set by the ledger (keel.ledger), not by the event's author

Types inside data are checked for numeric and enumerated fields: one line
`"input": "a lot"` must not be able to crash the scoreboard.
"""
from __future__ import annotations

import json
import time

TYPE_CHECKING = False  # typing costs 7 ms per import; annotations are deferred (from __future__ import annotations)
if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime
    from typing import Any

EVENT_TYPES: dict[str, str] = {
    # lifecycle
    "session.start": "session started (model, cwd, entry, memory_attached)",
    "session.stop": "session ended (reason, duration_s, turns)",
    # intent contract
    "task.open": "contract opened (goal, dod[], dod_hash, budget{}, forecast_p, zone[])",
    "task.close": "contract closed (outcome: verified|claimed|blocked|abandoned, dod_hash, evidence[])",
    "task.checkpoint": "budget checkpoint (spent{}, budget{}, decision)",
    # actions
    "tool.call": "tool call (tool, ok, ms, in_bytes, out_bytes, sig, path, turn, msg)",
    "usage": "token usage of one model response (model, input, output, cache_read, cache_create, req)",
    "prompt": "human message (chars, words, repeat_of)",
    # claims and proof
    "claim": "agent claims a result (kind: done|negated, words[], evidence_n, negated_n)",
    "verify": "check of a done-predicate (predicate, ok, evidence, dod_hash)",
    # owner
    "owner.verdict": "owner verdict on an artifact (artifact, verdict: accept|reject, reasons[] verbatim)",
    "owner.correction": "owner correction (kind: repeat_order|scope|quality|tone, text_hash)",
    # incidents and the cost of the layer
    "incident": "incident (cls, severity, repeat, detail)",
    "layer.cost": "cost of the layer itself (hook, ms, injected_bytes)",
}

REQUIRED = ("ts", "ev", "session", "project", "agent", "src", "data")

# field -> allowed types (None is always allowed: "not measured")
_INT_FIELDS: dict[str, tuple[str, ...]] = {
    "usage": ("input", "output", "cache_read", "cache_create", "thinking"),
    "tool.call": ("ms", "in_bytes", "out_bytes", "turn"),
    "prompt": ("chars", "words", "turn"),
    "claim": ("evidence_n", "tool_calls_in_turn", "negated_n", "chars"),
    "session.stop": ("duration_s", "turns"),
    "layer.cost": ("ms", "injected_bytes"),
}
_ENUM_FIELDS: dict[str, dict[str, set[str]]] = {
    "task.close": {"outcome": {"verified", "claimed", "blocked", "abandoned"}},
    "owner.verdict": {"verdict": {"accept", "reject"}},
    "claim": {"kind": {"done", "negated", "pass", "verified", "sent"}},
}


class SchemaError(ValueError):
    pass


def now_iso() -> str:
    """Local time with zone, second precision: the same shape as datetime.isoformat.
    Uses time.strftime so the adapter's hot path does not pay for importing datetime."""
    s = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return s[:-2] + ":" + s[-2:] if len(s) > 5 and (s[-5] in "+-") else s


def make_event(
    ev: str,
    *,
    session: str,
    project: str,
    data: dict[str, Any] | None = None,
    agent: str = "head",
    src: str = "cli",
    task: str | None = None,
    ts: str | None = None,
    seq: int | None = None,
) -> dict[str, Any]:
    e: dict[str, Any] = {
        "ts": ts or now_iso(),
        "ev": ev,
        "session": session,
        "project": project,
        "agent": agent,
        "src": src,
        "data": data or {},
    }
    if task is not None:
        e["task"] = task
    if seq is not None:
        e["seq"] = seq
    validate(e)
    return e


def validate(e: dict[str, Any]) -> None:
    """Check shape and types. An error means the event is not written, rather than written "somehow"."""
    if not isinstance(e, dict):
        raise SchemaError("event is not a dict")
    for k in REQUIRED:
        if k not in e:
            raise SchemaError(f"missing required field {k!r}")
    if e["ev"] not in EVENT_TYPES:
        raise SchemaError(f"unknown event type {e['ev']!r}")
    if not isinstance(e["data"], dict):
        raise SchemaError("data must be a dict")
    try:
        parse_ts(str(e["ts"]))
    except Exception as exc:  # noqa: BLE001
        raise SchemaError(f"ts is not ISO-8601: {e['ts']!r}") from exc
    if not str(e["session"]):
        raise SchemaError("empty session")
    d = e["data"]
    for f in _INT_FIELDS.get(e["ev"], ()):
        v = d.get(f)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
            raise SchemaError(f"{e['ev']}.{f} must be an int or None, not {type(v).__name__}")
    for f, allowed in _ENUM_FIELDS.get(e["ev"], {}).items():
        v = d.get(f)
        if v is not None and v not in allowed:
            raise SchemaError(f"{e['ev']}.{f}={v!r} is not one of {sorted(allowed)}")
    if e["ev"] == "tool.call" and "ok" in d and d["ok"] is not None and not isinstance(d["ok"], bool):
        raise SchemaError("tool.call.ok must be bool or None")


def safe_int(v: Any, default: int = 0) -> int:
    """A number from an event field that may have been corrupted: string, float, None."""
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def dumps(e: dict[str, Any]) -> str:
    return json.dumps(e, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def parse_ts(ts: str) -> "datetime":
    from datetime import datetime, timezone
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d
