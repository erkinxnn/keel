# -*- coding: utf-8 -*-
"""Scoreboard: how the vital signs are shown to the agent and to the owner.

Two formats:
    panel   compact, <= 20 lines, for injection into the context at session start; prints its
            own price in tokens (an estimate, including the price line), because the board is
            a cost too
    report  full markdown report with formulas, n, and the sources of thresholds
"""
from __future__ import annotations

from typing import Any

from .vitals import BANDS, NAMES

ORDER = list(NAMES.keys())


def fmt_value(v: dict[str, Any]) -> str:
    val = v["value"]
    if val is None or v.get("status") == "NO DATA":
        return "NO DATA"
    unit = v["unit"]
    if unit.startswith("share"):
        return f"{val * 100:.1f} %"
    if unit == "k/hour":
        return f"{val:.0f} k/h"
    if unit == "min":
        return f"{val:.0f} min"
    if unit == "Brier":
        return f"{val:.3f}"
    if unit == "per 100 calls":
        return f"{val:.2f}"
    if unit == "ms/call":
        return f"{val:.0f} ms"
    return f"{val:.0f}"


def token_estimate(text: str) -> int:
    # rough estimate for dense numeric text: ~3.2 chars per token
    return int(len(text) / 3.2) + 1


def render_panel(result: dict[str, Any], *, title: str = "KEEL SCOREBOARD", window_label: str = "",
                 show_balance: bool = False) -> str:
    lines = [f"{title} {window_label}".rstrip()]
    head = f"sessions {result['head_sessions']}, active hours {result['hours']}, events {result['events']}"
    if result.get("bad_events"):
        head += f", unreadable events {result['bad_events']}"
    lines.append(head)
    if show_balance:
        b = result.get("balance") or {}
        if b.get("value") is None:
            lines.append("Balance: NO DATA")
        else:
            lines.append(f"Balance: {b['value']:+d} = {b['formula']}")
    for vid in ORDER:
        v = result["vitals"].get(vid)
        if not v:
            continue
        short = vid.split("_")[0]
        s = v["status"]
        val = fmt_value(v)
        tail = f"n={v['n']}"
        if v.get("hint"):
            tail += f", {v['hint']}"
        if s == "NO DATA" and v.get("note") and not v.get("hint"):
            tail = v["note"]
        lines.append(f"{short} {v['name']}: {val} [{s}] ({tail})")
    text = "\n".join(lines)
    # the board's price is computed together with the price line (two iterations converge)
    cost = token_estimate(text + "\nboard cost ~0000 tokens")
    text += f"\nboard cost ~{cost} tokens"
    return text


def render_report(result: dict[str, Any], *, title: str, window_label: str = "") -> str:
    out = [f"# {title}", ""]
    if window_label:
        out.append(f"Window: {window_label}. ")
    out.append(f"Head sessions: {result['head_sessions']}, active hours: {result['hours']}, events in the ledger: {result['events']}"
               + (f", unreadable: {result['bad_events']}" if result.get("bad_events") else "") + ".")
    out.append("")
    out.append("| id | vital | value | n | status | threshold (source) |")
    out.append("|---|---|---|---|---|---|")
    for vid in ORDER:
        v = result["vitals"].get(vid)
        if not v:
            continue
        b = v["band"]
        if b.get("warn_below") is not None:
            thr = f"not below {b['warn_below']}"
        elif b.get("warn_above") is not None:
            thr = f"not above {b['warn_above']}"
        else:
            thr = "no threshold"
        src = (b.get("source") or "")[:90]
        st = v["status"] + (f" ({v['hint']})" if v.get("hint") else "")
        out.append(f"| {vid.split('_')[0]} | {v['name']} | {fmt_value(v)} | {v['n']} | {st} | {thr}; {b.get('status')}: {src} |")
    out.append("")
    out.append("## Details")
    for vid in ORDER:
        v = result["vitals"].get(vid)
        if not v:
            continue
        extra = ", ".join(f"{k}={val}" for k, val in v["extra"].items() if val not in (None, {}, []))
        note = v.get("note") or ""
        out.append(f"- **{vid.split('_')[0]} {v['name']}**: {fmt_value(v)}; n={v['n']}; {note} {('| ' + extra) if extra else ''}".rstrip())
    return "\n".join(out) + "\n"
