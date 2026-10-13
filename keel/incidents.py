# -*- coding: utf-8 -*-
"""Incident detectors over the event stream of one session.

Each detector is pure: it receives the list of a session's events (in time order) and returns a
list of `incident` events. No detector reads the disk or knows about hooks: that lets the same
detectors run over a live ledger, over a ledger built from history, and over a red-team fixture
in exactly the same way.

The `repeat` field of an incident is filled not by the detector but by `mark_repeats()`: a
repeat is a property of HISTORY (the class already happened in earlier sessions of the project),
not of the event.

Design notes: a loop requires either a failure or four repeats; a blind edit does not count for
auto-loaded files, a read shorter than 100 chars does not count as a read, and paths are
normalised; a stall is written once per wait, not once per parallel call; a fan-out is counted
per model message; negation cancels a completion word only within a window of three words to the
left, and questions are not claims; MEASURING_BECAME_COST is detected from layer.cost events.
"""
from __future__ import annotations

import json
import os
from collections import Counter, deque


from . import schema
from ._fast import median, sha256_hex
from .extract import norm_path, normalized_input

TYPE_CHECKING = False  # typing costs 7 ms per import; annotations are deferred (from __future__ import annotations)
if TYPE_CHECKING:  # pragma: no cover
    from typing import Any, Iterable

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "incident_classes.json"), "r", encoding="utf-8") as _fh:
    CLASSES: dict[str, dict[str, Any]] = json.load(_fh)["classes"]

# Flood threshold: 20,000 chars of one result (about 5,000 tokens at 4 chars per token).
# Calibrated on one user's history, where files of hundreds of KB were landing in context whole.
FLOOD_CHARS = 20_000
STALL_MIN = 30.0
LOOP_WINDOW = 10
LOOP_REPEATS = 3          # with at least one failure
LOOP_REPEATS_CLEAN = 4    # without failures
FANOUT_MIN = 4
ERROR_BURST = 3
MIN_READ_CHARS = 100
LAYER_TAX_MS = 500
# files the harness loads into context by itself, without a Read call
AUTO_LOADED = {"claude.md", "memory.md", "agents.md"}

DONE_WORDS_RE = (r"\b(done|ready|complete[d]?|finished|fixed|resolved|verified|sent|shipped|accepted|works|"
                 r"pass(?:ed)?)\b")
NEGATION_WORDS = {"not", "no", "yet", "never", "cannot", "isn't", "wasn't", "aren't", "weren't",
                  "haven't", "hasn't", "hadn't", "can't", "won't", "don't", "doesn't", "didn't"}
_RX: dict = {}


def _rx() -> dict:
    """`re` is needed by one detector (completion words, once per turn), but importing it costs
    24 ms on EVERY tool call. So the regexes are compiled on demand."""
    if not _RX:
        import re
        _RX["done"] = re.compile(DONE_WORDS_RE, re.IGNORECASE)
        _RX["token"] = re.compile(r"[\w']+", re.UNICODE)
        _RX["sent"] = re.compile(r"(?<=[.!?\n])\s+|\n+")
    return _RX


def _mk(base: dict[str, Any], cls: str, detail: dict[str, Any], ts: str | None = None) -> dict[str, Any]:
    c = CLASSES[cls]
    return schema.make_event(
        "incident",
        session=base["session"],
        project=base["project"],
        agent=base.get("agent", "head"),
        src=base.get("src", "cli"),
        task=base.get("task"),
        ts=ts or base["ts"],
        data={"cls": cls, "severity": c["severity"], "repeat": False, "detail": detail},
    )


def detect_loops(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    window: deque[tuple[str, bool]] = deque(maxlen=LOOP_WINDOW)
    fired: set[str] = set()
    for e in events:
        if e["ev"] != "tool.call":
            continue
        sig = str(e["data"].get("sig") or "")
        if not sig:
            continue
        failed = e["data"].get("ok") is False
        window.append((sig, failed))
        same = [f for s, f in window if s == sig]
        n = len(same)
        any_fail = any(same)
        if sig not in fired and ((any_fail and n >= LOOP_REPEATS) or n >= LOOP_REPEATS_CLEAN):
            fired.add(sig)
            out.append(_mk(e, "LOOP", {"tool": e["data"].get("tool"), "sig": sig[:12], "repeats": n, "with_failure": any_fail}))
    return out


def detect_blind_edits(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    read_paths: set[str] = set()
    written: set[str] = set()
    for e in events:
        if e["ev"] != "tool.call":
            continue
        tool = e["data"].get("tool")
        path = e["data"].get("path")
        if not path:
            continue
        p = norm_path(path)
        if tool in ("Read", "NotebookRead"):
            ob = e["data"].get("out_bytes")
            if ob is None or schema.safe_int(ob) >= MIN_READ_CHARS:
                read_paths.add(p)
        elif tool in ("Edit", "MultiEdit", "NotebookEdit"):
            base = p.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if p not in read_paths and p not in written and base not in AUTO_LOADED:
                out.append(_mk(e, "BLIND_EDIT", {"tool": tool, "path": path}))
            written.add(p)
        elif tool == "Write":
            written.add(p)
    return out


def detect_floods(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        if e["ev"] == "tool.call" and schema.safe_int(e["data"].get("out_bytes")) > FLOOD_CHARS:
            out.append(_mk(e, "FLOOD", {"tool": e["data"].get("tool"), "out_bytes": schema.safe_int(e["data"]["out_bytes"])}))
    return out


def detect_stalls(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One incident per wait: parallel calls of one message wait the same interval and give the
    same pair (start second, minutes)."""
    out = []
    seen: set[tuple[str, int]] = set()
    for e in events:
        if e["ev"] != "tool.call":
            continue
        ms = e["data"].get("ms")
        if ms is None:
            continue
        minutes = float(schema.safe_int(ms)) / 60000.0
        if minutes > STALL_MIN:
            key = (str(e["ts"])[:19], int(round(minutes)))
            if key in seen:
                continue
            seen.add(key)
            out.append(_mk(e, "STALL", {"tool": e["data"].get("tool"), "minutes": round(minutes, 1)}))
    return out


def detect_error_bursts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    streak = 0
    for e in events:
        if e["ev"] != "tool.call":
            continue
        if e["data"].get("ok") is False:
            streak += 1
            if streak == ERROR_BURST:
                out.append(_mk(e, "TOOL_ERROR_BURST", {"tool": e["data"].get("tool"), "streak": streak}))
        else:
            streak = 0
    return out


def detect_fanout(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fan-out: >= FANOUT_MIN Agent calls in ONE model message (field msg; in history it is the
    requestId, in the live hook a 15-second window)."""
    out = []
    by_msg: dict[Any, list[dict[str, Any]]] = {}
    for e in events:
        if e["ev"] == "tool.call" and e["data"].get("tool") == "Agent":
            key = e["data"].get("msg")
            if key is None:
                continue
            by_msg.setdefault(key, []).append(e)
    for key, calls in by_msg.items():
        if len(calls) >= FANOUT_MIN:
            out.append(_mk(calls[0], "FANOUT", {"msg": str(key)[:16], "agents": len(calls), "turn": calls[0]["data"].get("turn")}))
    return out


def detect_unverified_claims(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A claim of kind=done with evidence_n == 0 and tool_calls_in_turn == 0 is a claim without evidence."""
    out = []
    for e in events:
        if e["ev"] != "claim" or e["data"].get("kind") not in ("done", None):
            continue
        d = e["data"]
        if schema.safe_int(d.get("evidence_n")) == 0 and schema.safe_int(d.get("tool_calls_in_turn")) == 0 and d.get("words"):
            out.append(_mk(e, "UNVERIFIED_CLAIM", {"kind": d.get("kind"), "words": d.get("words")}))
    return out


def detect_repeat_orders(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        if e["ev"] == "prompt" and e["data"].get("repeat_of"):
            out.append(_mk(e, "REPEAT_ORDER", {"repeat_of": e["data"]["repeat_of"], "words": e["data"].get("words")}))
    return out


def detect_memory_lost(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        if e["ev"] == "session.start" and e["data"].get("memory_attached") is False:
            out.append(_mk(e, "MEMORY_LOST", {"cwd": e["data"].get("cwd")}))
    return out


def detect_measuring_cost(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The layer itself became a cost: the median internal time of the adapters is above LAYER_TAX_MS."""
    costs = [e for e in events if e["ev"] == "layer.cost" and e["data"].get("kind") != "wall"]
    if len(costs) < 5:
        return []
    med = median(float(schema.safe_int(c["data"].get("ms"))) for c in costs)
    if med > LAYER_TAX_MS:
        return [_mk(costs[-1], "MEASURING_BECAME_COST", {"median_ms": round(med), "calls": len(costs)})]
    return []


DETECTORS = (
    detect_loops,
    detect_blind_edits,
    detect_floods,
    detect_stalls,
    detect_error_bursts,
    detect_fanout,
    detect_unverified_claims,
    detect_repeat_orders,
    detect_memory_lost,
    detect_measuring_cost,
)


def detect_all(session_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for det in DETECTORS:
        found.extend(det(session_events))
    found.sort(key=lambda x: x["ts"])
    return found


def mark_repeats(incidents: list[dict[str, Any]], history_classes: Iterable[str]) -> list[dict[str, Any]]:
    """Repeat = the class was already seen in history (earlier sessions) before this incident."""
    seen: set[str] = set(history_classes)
    for inc in incidents:
        cls = inc["data"]["cls"]
        inc["data"]["repeat"] = cls in seen
        seen.add(cls)
    return incidents


def signature(tool: str, tool_input: Any) -> str:
    """Call signature for the loop detector: tool + canonical input."""
    tin = normalized_input(tool, tool_input)
    try:
        canon = json.dumps(tin, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        canon = str(tin)
    return sha256_hex(f"{tool}\x00{canon}".encode("utf-8", "replace"))[:40]


def claim_scan(text: str) -> tuple[list[str], int]:
    """Completion words in a text and the number cancelled by negation.
    Negation counts only within a window of three words to the left of the completion word;
    interrogative sentences are not claims."""
    words: list[str] = []
    negated = 0
    rx = _rx()
    text = (text or "").replace(chr(0x2019), "'")  # typographic apostrophe (U+2019) -> ASCII, so "isn't" matches
    for sent in rx["sent"].split(text):
        s = sent.strip()
        if not s:
            continue
        if s.endswith("?"):
            continue
        tokens = [t.lower() for t in rx["token"].findall(s)]
        for i, tok in enumerate(tokens):
            if not rx["done"].fullmatch(tok):
                continue
            left = tokens[max(0, i - 3):i]
            if any(w in NEGATION_WORDS for w in left):
                negated += 1
            else:
                words.append(tok)
    return words, negated


def claim_words(text: str) -> list[str]:
    return claim_scan(text)[0]
