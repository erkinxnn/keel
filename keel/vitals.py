# -*- coding: utf-8 -*-
"""Agent vital signs: computed ONLY from ledger events.

Not a "score out of 10". A living organism has no single health number: it has pulse, pressure,
temperature, each with its own range and its own instrument. Same here: every vital has a source
event, a formula, a number of observations n, and a band with a recorded source for its threshold.

Statuses:
    NO DATA   no source events; the board prints what to start recording
    OK        inside the band (the band has status=norm)
    WARN      outside the normal band
    REF       the band is reference only (a threshold with no measurement at both ends): it must
              not be painted red, it is printed with the value as "above/below the reference threshold"

Design notes: V1 counts owner.verdict accepts whose source is not the CLI (tg, owner,
owner-file); the CLI cannot write those sources, but the ledger is not authenticated, so V1 is a
count of claimed accepts, not of signed ones. V2 replays the contracts in ledger order (shared with
`keel.contract.close_task`): a close is proven only if its open exists and the latest verify of
every predicate between the open and the close passed; for a time window the contract events from
before the window are passed in `contract_events`. Tool calls observed twice (live hook and
subagent transcript) are counted once by `tool_use_id`; V8 counts distinct files; V9 is a union of intervals, one
incident per wait; V14 recognises a "coin flip" forecaster and prints the mean p; corrupted
numbers do not crash the board, they are counted; unfinished calls are reported separately; the
board's own cost is computed including the cost line.

V4 time base: the denominator is ACTIVE time, not the sum of session durations. Within one
session, the gaps between consecutive events are summed, and a gap longer than IDLE_GAP_S
(10 minutes) counts as idle and adds nothing. A session left open for 25 hours with 40 minutes
of work therefore contributes about 40 minutes. Sessions run in parallel, so their active times
add up (more than 24 h per calendar day is possible).

Usage de-duplication: a `usage` event may carry the id of the model request (`req`). When it
does, several events with the same id are counted once. This matters because a subagent
transcript that is parsed twice (it grew between two SubagentStop calls) writes its earlier
usage rows again. Events written without `req` (older ledgers) cannot be de-duplicated and are
all counted; the count of dropped duplicates is printed in V4's extra.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from . import schema
from .contract import TRUSTED_VERDICT_SRC, replay

# reference threshold for a full adapter call (interpreter + imports + work), ms.
# Calibrated on one machine (base cost of an empty `python -S -E` process about 87 ms): the layer
# answers for its own time, the full call is observed.
WALL_BAND_MS = 250

# a gap between two consecutive events of a session longer than this is idle time (seconds)
IDLE_GAP_S = 600.0

_HERE = Path(__file__).resolve().parent
BANDS: dict[str, dict[str, Any]] = json.loads((_HERE / "vitals_bands.json").read_text(encoding="utf-8"))["bands"]
_i = schema.safe_int

NAMES: dict[str, tuple[str, str]] = {
    "V1_accepted": ("Claimed owner accepts", "count"),
    "V2_verified_done": ("Proven done", "share"),
    "V3_evidence_ratio": ("Claims with evidence", "share"),
    "V4_tokens_per_hour": ("Token burn", "k/hour"),
    "V5_flood_share": ("Context flood", "share of output"),
    "V6_tool_error_rate": ("Tool errors", "share"),
    "V7_loops_per_100": ("Retry loops", "per 100 calls"),
    "V8_blind_edit_rate": ("Blind edits", "share of files"),
    "V9_stall_minutes": ("Idle waiting", "min"),
    "V10_fanout_turns": ("Subagent fan-out", "messages"),
    "V11_owner_repeat_share": ("Owner repeating orders", "share"),
    "V12_repeat_incident_share": ("Repeat of known incident", "share"),
    "V13_layer_tax_ms": ("Layer tax", "ms/call"),
    "V14_calibration_brier": ("Forecast calibration", "Brier"),
}


def _v(vid: str, value: float | None, n: int, *, note: str = "", extra: dict[str, Any] | None = None,
       force_no_data: str | None = None) -> dict[str, Any]:
    name, unit = NAMES[vid]
    band = BANDS.get(vid, {})
    status = "NO DATA"
    hint = ""
    if force_no_data:
        hint = force_no_data
    elif value is not None and n > 0:
        min_n = band.get("min_n")
        if min_n and n < min_n:
            hint = f"too few points: n={n} < {min_n}"
        else:
            beyond = False
            inclusive = bool(band.get("inclusive"))
            if band.get("direction") == "higher" and band.get("warn_below") is not None:
                beyond = value <= band["warn_below"] if inclusive else value < band["warn_below"]
            if band.get("direction") == "lower" and band.get("warn_above") is not None:
                beyond = value >= band["warn_above"] if inclusive else value > band["warn_above"]
            if band.get("status") == "norm":
                status = "WARN" if beyond else "OK"
            else:
                status = "REF"
                if beyond:
                    hint = "beyond the reference threshold"
    return {
        "id": vid, "name": name, "unit": unit, "value": value, "n": n, "status": status,
        "band": {k: band.get(k) for k in ("direction", "warn_below", "warn_above", "status", "source", "min_n", "inclusive")},
        "note": note, "hint": hint, "extra": extra or {},
    }


def _median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _p90(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.9 * (len(s) - 1))))]


def _union_minutes(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    iv = sorted(intervals)
    total = 0.0
    cur_s, cur_e = iv[0]
    for s, e in iv[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return total / 60.0


def active_seconds(events: Iterable[dict[str, Any]], idle_gap_s: float = IDLE_GAP_S) -> float:
    """Active time of the events: per session, the sum of gaps between consecutive events,
    where a gap longer than `idle_gap_s` is idle and adds nothing. Benchmark records
    (layer.cost with kind=wall) belong to no session and are skipped."""
    per_session: dict[str, list[float]] = defaultdict(list)
    for e in events:
        if e["ev"] == "layer.cost" and e["data"].get("kind") == "wall":
            continue
        try:
            t = schema.parse_ts(e["ts"]).timestamp()
        except Exception:  # noqa: BLE001
            continue
        per_session[str(e["session"])].append(t)
    total = 0.0
    for stamps in per_session.values():
        stamps.sort()
        for a, b in zip(stamps, stamps[1:]):
            gap = b - a
            if 0.0 <= gap <= idle_gap_s:
                total += gap
    return total


def _ekey(e: dict[str, Any]) -> Any:
    return e.get("h") or id(e)


def compute(events: Iterable[dict[str, Any]], contract_events: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    """events: the events of one window (already filtered by time and project), in ledger order.
    contract_events: task.open/verify/task.close events of the WHOLE ledger in ledger order, for a
    window report (a close inside the window may belong to an open before it). None = use `events`."""
    ev = []
    bad_events = 0
    seen_tu: dict[tuple[str, str], int] = {}
    tool_dups = 0
    for e in events:
        if not isinstance(e, dict) or not isinstance(e.get("data"), dict) or "ev" not in e \
                or "session" not in e or "ts" not in e:
            bad_events += 1
            continue
        if e["ev"] == "tool.call" and e["data"].get("tool_use_id"):
            k = (str(e["session"]), str(e["data"]["tool_use_id"]))
            j = seen_tu.get(k)
            if j is not None:
                tool_dups += 1
                # a finished record (ok True/False) replaces an unfinished one (ok None) whole, so the
                # result fields (ok, interrupted, out_bytes, ms) stay those of one record
                if ev[j]["data"].get("ok") is None and e["data"].get("ok") is not None:
                    ev[j] = e
                continue
            seen_tu[k] = len(ev)
        ev.append(e)
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in ev:
        by[e["ev"]].append(e)

    sessions = {e["session"] for e in ev}
    head_sessions = {e["session"] for e in by["session.start"] if e.get("agent", "head") == "head"}
    durations = [float(_i(e["data"].get("duration_s"))) for e in by["session.stop"] if e.get("agent", "head") == "head"]
    wall_clock_hours = sum(durations) / 3600.0   # old time base, kept for transparency only
    active_s = active_seconds(ev)
    hours = active_s / 3600.0                      # the time base of V4

    # ---- tokens (de-duplicated by model request id when the event carries one)
    tok: Counter[str] = Counter()
    tok_sub = 0
    per_session_tokens: Counter[str] = Counter()
    ctx: list[float] = []
    seen_req: set[str] = set()
    usage_dups = 0
    for e in by["usage"]:
        d = e["data"]
        req = d.get("req")
        if req:
            if str(req) in seen_req:
                usage_dups += 1
                continue
            seen_req.add(str(req))
        total = _i(d.get("input")) + _i(d.get("output")) + _i(d.get("cache_read")) + _i(d.get("cache_create"))
        tok["total"] += total
        tok["input"] += _i(d.get("input"))
        tok["output"] += _i(d.get("output"))
        tok["cache_read"] += _i(d.get("cache_read"))
        tok["cache_create"] += _i(d.get("cache_create"))
        tok["thinking"] += _i(d.get("thinking"))
        tok["calls"] += 1
        per_session_tokens[e["session"]] += total
        ctx.append(float(_i(d.get("cache_read")) + _i(d.get("input"))))
        if e.get("agent", "head") != "head":
            tok_sub += total

    # ---- tools
    calls = by["tool.call"]
    finished = [c for c in calls if c["data"].get("ok") is not None]
    unfinished = len(calls) - len(finished)
    n_calls = len(finished)
    ok_true = sum(1 for c in finished if c["data"].get("ok") is True)
    interrupted = sum(1 for c in finished if c["data"].get("ok") is False and c["data"].get("interrupted"))
    ok_false = sum(1 for c in finished if c["data"].get("ok") is False and not c["data"].get("interrupted"))
    out_total = sum(_i(c["data"].get("out_bytes")) for c in finished)
    out_flood = sum(_i(c["data"].get("out_bytes")) for c in finished if _i(c["data"].get("out_bytes")) > 20000)
    n_flood = sum(1 for c in finished if _i(c["data"].get("out_bytes")) > 20000)
    edit_files = {str(c["data"].get("path")).replace("\\", "/").lower() for c in calls
                  if c["data"].get("tool") in ("Edit", "MultiEdit", "NotebookEdit") and c["data"].get("path")}
    n_edit_calls = sum(1 for c in calls if c["data"].get("tool") in ("Edit", "MultiEdit", "NotebookEdit"))
    reads = sum(1 for c in calls if c["data"].get("tool") == "Read")
    tool_mix = Counter(c["data"].get("tool") for c in calls)

    # ---- incidents
    incs = by["incident"]
    cls_count = Counter(i["data"].get("cls") for i in incs)
    repeats = sum(1 for i in incs if i["data"].get("repeat"))
    stall_iv: list[tuple[float, float]] = []
    stall_keys: set[tuple[str, int]] = set()
    for i in incs:
        if i["data"].get("cls") != "STALL":
            continue
        try:
            start = schema.parse_ts(i["ts"]).timestamp()
        except Exception:  # noqa: BLE001
            bad_events += 1
            continue
        det = i["data"].get("detail")
        minutes = max(schema.safe_float(det.get("minutes")) if isinstance(det, dict) else 0.0, 0.0)
        stall_iv.append((start, start + minutes * 60.0))
        stall_keys.add((str(i["ts"])[:19], int(round(minutes))))
    stall_union_min = _union_minutes(stall_iv)
    blind_files = {str(i["data"]["detail"].get("path")).replace("\\", "/").lower()
                   for i in incs if i["data"].get("cls") == "BLIND_EDIT" and isinstance(i["data"].get("detail"), dict)}

    # ---- claims
    claims_done = [c for c in by["claim"] if c["data"].get("kind") in ("done", None)]
    claims_with_ev = sum(1 for c in claims_done if _i(c["data"].get("evidence_n")) > 0)
    negated = sum(_i(c["data"].get("negated_n")) for c in by["claim"])

    # ---- contracts: replayed in ledger order, the same logic as contract.close_task
    closes = [c for c in by["task.close"] if c.get("task") is not None]
    if contract_events is None:
        replayed = replay(ev)[0]
    else:
        window_keys = {_ekey(c) for c in closes}
        replayed = [r for r in replay(contract_events)[0] if _ekey(r[0]) in window_keys]
    verified = 0
    unproven_verified = 0
    briers: list[float] = []
    ps: list[float] = []
    for c, _opened, proven in replayed:
        d = c["data"]
        is_ver = False
        if d.get("outcome") == "verified":
            if proven:
                is_ver = True
                verified += 1
            else:
                unproven_verified += 1
        p = d.get("forecast_p")
        if p is not None:
            try:
                pf = float(p)
            except (TypeError, ValueError):
                bad_events += 1
                continue
            ps.append(pf)
            briers.append((pf - (1.0 if is_ver else 0.0)) ** 2)

    # ---- owner
    verdicts = by["owner.verdict"]
    trusted = [v for v in verdicts if v.get("src") in TRUSTED_VERDICT_SRC]
    accepted = sum(1 for v in trusted if v["data"].get("verdict") == "accept")
    untrusted_acc = sum(1 for v in verdicts if v.get("src") not in TRUSTED_VERDICT_SRC and v["data"].get("verdict") == "accept")
    untrusted_rej = sum(1 for v in verdicts if v.get("src") not in TRUSTED_VERDICT_SRC and v["data"].get("verdict") == "reject")
    prompts = by["prompt"]
    repeats_prompt = sum(1 for p in prompts if p["data"].get("repeat_of"))

    # ---- layer tax
    layer = by["layer.cost"]
    # kind="wall" is written by a benchmarking tool: it is the FULL adapter call (interpreter
    # start + imports + work), not the internal time. The two must not be mixed.
    wall = [l for l in layer if l["data"].get("kind") == "wall"]
    layer = [l for l in layer if l["data"].get("kind") != "wall"]
    layer_ms = [float(_i(l["data"].get("ms"))) for l in layer]
    injected = sum(_i(l["data"].get("injected_bytes")) for l in layer)
    last_ts = max((str(w.get("ts")) for w in wall), default=None)
    wall_last = [w for w in wall if str(w.get("ts")) == last_ts]  # only the latest benchmark run
    # the per-call tax is the adapter that runs on every tool call in the standard registration.
    # PreToolUse is not registered (tools/install_hooks.py), so that is PostToolUse.
    wall_per_call = [float(_i(w["data"].get("ms"))) for w in wall_last if w["data"].get("hook") == "post_tool_use"]
    if not wall_per_call:
        wall_per_call = [float(_i(w["data"].get("ms"))) for w in wall_last if w["data"].get("freq") == "call"]
    wall_floor = [float(_i(w["data"].get("floor_ms"))) for w in wall_last if _i(w["data"].get("floor_ms"))]

    vit: dict[str, Any] = {}
    vit["V1_accepted"] = _v(
        "V1_accepted", float(accepted) if trusted else None, len(trusted),
        note=("claimed: written by an owner-side integration through keel.contract.record_verdict; the CLI cannot write it, the ledger does not authenticate it"
              if trusted else ("verdicts exist only from the CLI (src=cli), they do not count"
                               if verdicts else "no owner.verdict from an owner-side integration (KEEL ships none)")),
        extra={"rejected": len(trusted) - accepted, "untrusted_accepts": untrusted_acc, "untrusted_rejects": untrusted_rej},
    )
    vit["V2_verified_done"] = _v(
        "V2_verified_done", (verified / len(closes)) if closes else None, len(closes),
        note="" if closes else "no task.open/task.close contracts: open tasks with done-predicates",
        extra={"verified": verified, "unproven_verified": unproven_verified,
               "claimed": sum(1 for c in closes if c["data"].get("outcome") == "claimed"),
               "proxy_claims_with_tool_evidence": (round(claims_with_ev / len(claims_done), 3) if claims_done else None)},
    )
    vit["V3_evidence_ratio"] = _v(
        "V3_evidence_ratio", (claims_with_ev / len(claims_done)) if claims_done else None, len(claims_done),
        note="class-B approximation: evidence = a successful tool result in the same turn" if claims_done else "no claim events",
        extra={"claims": len(claims_done), "claims_without_any_tool": len(claims_done) - claims_with_ev, "negated_by_words": negated},
    )
    vit["V4_tokens_per_hour"] = _v(
        "V4_tokens_per_hour", (tok["total"] / hours / 1000.0) if hours > 0 and tok["total"] else None, len(head_sessions),
        note="all tokens incl. cache; hours = active time (gaps over 10 min are idle); parallel sessions add up",
        extra={"total": tok["total"], "input": tok["input"], "output": tok["output"], "cache_read": tok["cache_read"],
               "cache_create": tok["cache_create"], "thinking": tok["thinking"],
               "subagent_share": (round(tok_sub / tok["total"], 3) if tok["total"] else None),
               "hours": round(hours, 1), "active_seconds": round(active_s), "idle_gap_s": int(IDLE_GAP_S),
               "session_wall_clock_hours": round(wall_clock_hours, 1),
               "sessions": len(head_sessions), "model_calls": tok["calls"], "usage_duplicates_dropped": usage_dups,
               "context_per_call_median_k": (round(_median(ctx) / 1000.0) if ctx else None),
               "context_per_call_p90_k": (round(_p90(ctx) / 1000.0) if ctx else None),
               "per_session_median_k": (round(_median(list(per_session_tokens.values())) / 1000.0, 1) if per_session_tokens else None),
               "per_session_p90_k": (round(_p90(list(per_session_tokens.values())) / 1000.0, 1) if per_session_tokens else None)},
    )
    vit["V5_flood_share"] = _v(
        "V5_flood_share", (out_flood / out_total) if out_total else None, n_calls,
        extra={"floods": n_flood, "out_total_mb": round(out_total / 1e6, 1), "flood_mb": round(out_flood / 1e6, 1), "reads": reads},
    )
    vit["V6_tool_error_rate"] = _v(
        "V6_tool_error_rate", (ok_false / (ok_true + ok_false)) if (ok_true + ok_false) else None, ok_true + ok_false,
        note="live failures need the PostToolUseFailure adapter; user interrupts are not errors",
        extra={"errors": ok_false, "interrupted": interrupted, "calls": len(calls), "unfinished": unfinished,
               "duplicates_dropped": tool_dups, "tool_mix": dict(tool_mix.most_common(8))},
    )
    vit["V7_loops_per_100"] = _v(
        "V7_loops_per_100", (cls_count["LOOP"] / n_calls * 100.0) if n_calls else None, n_calls,
        extra={"loops": cls_count["LOOP"]},
    )
    vit["V8_blind_edit_rate"] = _v(
        "V8_blind_edit_rate", (len(blind_files) / len(edit_files)) if edit_files else None, len(edit_files),
        note="share of edited files that were not read in the session; auto-loaded files (CLAUDE.md, MEMORY.md) are excluded",
        extra={"blind_files": len(blind_files), "edited_files": len(edit_files), "blind_events": cls_count["BLIND_EDIT"], "edit_calls": n_edit_calls},
    )
    vit["V9_stall_minutes"] = _v(
        "V9_stall_minutes", stall_union_min if n_calls else None, len(stall_keys),
        note="union of intervals where a wait between a call and its result exceeded 30 min; n = number of waits",
        extra={"waits": len(stall_keys), "unfinished_calls": unfinished,
               "per_session_min": (round(stall_union_min / len(head_sessions), 1) if head_sessions else None)},
    )
    vit["V10_fanout_turns"] = _v(
        "V10_fanout_turns", float(cls_count["FANOUT"]) if n_calls else None, len(head_sessions),
        note="model messages with 4+ Agent calls at once",
        extra={"agent_calls": tool_mix.get("Agent", 0)},
    )
    vit["V11_owner_repeat_share"] = _v(
        "V11_owner_repeat_share", (repeats_prompt / len(prompts)) if prompts else None, len(prompts),
        note="repeat by word overlap (Jaccard >= 0.8, >= 6 words, 14 days): stricter than a semantic count; harness plumbing messages are excluded",
        extra={"repeats": repeats_prompt, "prompts": len(prompts)},
    )
    vit["V12_repeat_incident_share"] = _v(
        "V12_repeat_incident_share", (repeats / len(incs)) if incs else None, len(incs),
        note="v0.1: repeat = the class already happened in the project history; saturates on a mature history",
        extra={"incidents": len(incs), "repeats": repeats, "by_class": dict(cls_count.most_common())},
    )
    wall_ms = round(_median(wall_per_call)) if wall_per_call else None
    vit["V13_layer_tax_ms"] = _v(
        "V13_layer_tax_ms", _median(layer_ms) if layer_ms else None, len(layer_ms),
        note=(("internal adapter time up to the ledger write (lock, write, fsync not included); full call " + str(wall_ms) + " ms (interpreter start and imports, from a benchmark run)")
              if (layer_ms and wall_ms is not None) else
              ("internal adapter time; the full call was not benchmarked (no layer.cost events with kind=wall)" if layer_ms
               else "the layer has not run live yet: layer.cost is written by the hooks")),
        extra={"injected_bytes": injected, "wall_ms": wall_ms,
               "floor_ms": round(_median(wall_floor)) if wall_floor else None,
               "wall_measured_at": last_ts, "wall_events": len(wall)},
    )
    wall_med = round(_median([float(_i(w["data"].get("med_ms"))) for w in wall_last
                              if _i(w["data"].get("med_ms")) and w["data"].get("hook") == "post_tool_use"])) \
        if any(w["data"].get("hook") == "post_tool_use" and _i(w["data"].get("med_ms")) for w in wall_last) else None
    vit["V13_layer_tax_ms"]["extra"]["wall_median_ms"] = wall_med
    vit["V13_layer_tax_ms"]["extra"]["wall_band_ms"] = WALL_BAND_MS
    if wall_med is not None and wall_med > WALL_BAND_MS:
        # second threshold: not "our work" but what the user pays in total
        vit["V13_layer_tax_ms"]["hint"] = ((vit["V13_layer_tax_ms"]["hint"] + ", ") if vit["V13_layer_tax_ms"]["hint"] else "") \
            + f"full call {wall_med} ms is beyond the reference threshold {WALL_BAND_MS} ms"
    if wall_ms is not None and vit["V13_layer_tax_ms"]["value"] is not None:
        # the board shows both the internal time and what the user actually pays:
        # interpreter start and imports (law 5: the layer prints its price next to its output)
        h = vit["V13_layer_tax_ms"]["hint"]
        vit["V13_layer_tax_ms"]["hint"] = (h + ", " if h else "") + f"full call {wall_ms} ms"
    coin_share = (sum(1 for p in ps if 0.45 <= p <= 0.55) / len(ps)) if ps else None
    vit["V14_calibration_brier"] = _v(
        "V14_calibration_brier", (sum(briers) / len(briers)) if briers else None, len(briers),
        note="" if briers else "no p_success forecasts in task.open",
        extra={"mean_p": (round(sum(ps) / len(ps), 3) if ps else None), "coin_share": (round(coin_share, 2) if coin_share is not None else None),
               "base_rate": (round(verified / len(closes), 3) if closes else None)},
        force_no_data=("no forecast: the share of p in 0.45..0.55 is %d %%, that is a coin flip" % round(coin_share * 100)) if (coin_share is not None and coin_share >= 0.5 and len(ps) >= 3) else None,
    )

    # ---- balance (points): only a sum of events with fixed weights, the formula is printed
    balance: dict[str, Any] = {"value": None, "formula": "NO DATA: no events"}
    if ev:
        pen_classes = ("UNVERIFIED_CLAIM", "STALL", "BLIND_EDIT", "FANOUT")
        pen = {c: cls_count[c] for c in pen_classes}
        pen_repeat = sum(1 for i in incs if i["data"].get("cls") in pen_classes and i["data"].get("repeat"))
        value = 3 * accepted + verified - sum(pen.values()) - pen_repeat
        formula = (f"3*accepted({accepted}) + proven({verified}) - done-without-evidence({pen['UNVERIFIED_CLAIM']}) "
                   f"- idle({pen['STALL']}) - blind edits({pen['BLIND_EDIT']}) - fan-out({pen['FANOUT']}) "
                   f"- repeats among them({pen_repeat})")
        balance = {"value": value, "formula": formula}
    return {
        "sessions": len(sessions), "head_sessions": len(head_sessions), "hours": round(hours, 1),
        "events": len(ev), "bad_events": bad_events, "vitals": vit, "balance": balance,
    }


def status_counts(result: dict[str, Any]) -> Counter[str]:
    return Counter(v["status"] for v in result["vitals"].values())
