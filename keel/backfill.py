# -*- coding: utf-8 -*-
"""A ledger from HISTORY: parsing Claude Code transcripts (~/.claude/projects/*/*.jsonl).

Why: metrics must not be taken on faith from a single session. Backfill builds a ledger from
all past sessions of all projects and gives the vital signs a baseline ("where we were"),
without installing a single hook into live projects.

What is extracted from a transcript (only numbers and hashes; the owner's text is NOT written
to the ledger):
    session.start / session.stop   session boundaries, model, directory, whether memory is attached
    usage                          token usage of one response (de-duplicated by requestId: one
                                   response may be split over several transcript lines; the
                                   request id is kept in the `req` field)
    tool.call                      tool, ok/error, duration, result size, call signature (for
                                   loops), path (for blind edits), turn, msg = the model message
                                   (for fan-out)
    prompt                         a human message: number of chars and words, whether it repeats
                                   an earlier one; harness plumbing (compaction summaries,
                                   interruptions) does not count as a human message
    claim                          the final text of a turn with completion words, and how much
                                   evidence (successful tool results) there was in that turn;
                                   plus the number of words cancelled by negation
    incident                       the keel.incidents detectors per session + a repeat mark by
                                   project history (sessions in chronological order)

Honest limits:
    * "evidence" here = a successful tool result in the same turn. This is a class-B
      APPROXIMATION: a live ledger with task contracts gives a strict count (verify events),
      backfill cannot.
    * an owner repeat is counted by word overlap (Jaccard >= 0.8 with >= 6 words in 14 days).
      This is strict: it undercounts semantic repeats.
    * permission waits are not marked in a transcript; STALL is taken as a pause between a tool
      call and its result longer than 30 minutes.
    * a negative call-to-result interval (clocks diverged) is written as ms=None with a skew
      flag, not turned into zero.
    * the ledger is written to a temporary file and replaces the previous one only whole; with
      `--days` the file name gets a window suffix so a full ledger is not overwritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import incidents as inc
from . import schema
from .extract import content_len, in_bytes, is_human_prompt, tool_path
from .ledger import Ledger

REPEAT_WINDOW_DAYS = 14
REPEAT_JACCARD = 0.8
REPEAT_MIN_WORDS = 6
_WORD = re.compile(r"[\w\-]{3,}", re.UNICODE)


def _ts(rec: dict[str, Any]) -> datetime | None:
    t = rec.get("timestamp")
    if not t:
        return None
    try:
        return schema.parse_ts(t)
    except Exception:  # noqa: BLE001
        return None


def _iso(d: datetime) -> str:
    return d.astimezone().isoformat(timespec="seconds")


def _human_text(content: Any) -> str | None:
    """Text of a human message, or None if it is a tool result or plumbing."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    return text.strip() if is_human_prompt(text) else None


def _wordset(text: str) -> frozenset[str]:
    return frozenset(w.lower() for w in _WORD.findall(text))


def _project_name_from_cwd(cwd: str | None, fallback: str) -> str:
    if not cwd:
        return fallback
    base = os.path.basename(cwd.rstrip("\\/"))
    return base or fallback


class _PromptHistory:
    """History of a project's human messages, for finding repeated orders."""

    def __init__(self) -> None:
        self.items: list[tuple[datetime, str, frozenset[str], str]] = []  # ts, hash, words, session

    def check(self, ts: datetime, text: str, session: str) -> str | None:
        h = hashlib.sha1(text.strip().lower().encode("utf-8", "replace")).hexdigest()[:12]
        words = _wordset(text)
        found: str | None = None
        if len(words) >= REPEAT_MIN_WORDS:
            lo = ts - timedelta(days=REPEAT_WINDOW_DAYS)
            for pts, ph, pw, psess in reversed(self.items):
                if pts < lo:
                    break
                if ph == h:
                    found = ph
                    break
                inter = len(words & pw)
                union = len(words | pw)
                if union and inter / union >= REPEAT_JACCARD:
                    found = ph
                    break
        self.items.append((ts, h, words, session))
        if len(self.items) > 4000:
            del self.items[:1000]
        return found


def parse_session(
    path: Path,
    *,
    project: str,
    memory_attached: bool | None,
    prompt_history: _PromptHistory,
) -> list[dict[str, Any]]:
    """One transcript -> events (without incidents). Streaming, line by line."""
    events: list[dict[str, Any]] = []
    session_id = path.stem
    # A subagent transcript lives in `<session>/subagents/agent-*.jsonl`: its usage and calls
    # belong to the parent session, the agent is marked as "sub".
    is_sub_file = path.name.startswith("agent-") or "subagents" in path.parts
    if is_sub_file:
        session_id = path.parent.parent.name if path.parent.name == "subagents" else path.parent.name + ":" + path.stem
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    cwd: str | None = None
    model: str | None = None
    entry: str | None = None
    seen_req: set[str] = set()
    pending: dict[str, dict[str, Any]] = {}
    turn = 0
    turn_calls = 0
    turn_evidence = 0
    final_text_parts: list[str] = []
    final_req: str | None = None
    src = "backfill"

    def close_turn(ts: datetime) -> None:
        nonlocal turn_calls, turn_evidence, final_text_parts, final_req
        text = "\n".join(final_text_parts)
        words, negated = inc.claim_scan(text)
        if (words or negated) and turn > 0:
            events.append(
                schema.make_event(
                    "claim", session=session_id, project=project, agent="head", src=src, ts=_iso(ts),
                    data={
                        "kind": "done" if words else "negated",
                        "words": sorted(set(words))[:6],
                        "negated_n": negated,
                        "evidence_n": turn_evidence,
                        "tool_calls_in_turn": turn_calls,
                        "turn": turn,
                        "text_hash": hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12],
                        "chars": len(text),
                    },
                )
            )
        turn_calls = 0
        turn_evidence = 0
        final_text_parts = []
        final_req = None

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            if rtype not in ("user", "assistant"):
                continue
            ts = _ts(rec)
            if ts is None:
                continue
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            if cwd is None and rec.get("cwd"):
                cwd = rec.get("cwd")
            if entry is None and rec.get("entrypoint"):
                entry = rec.get("entrypoint")
            if rec.get("sessionId") and not is_sub_file:
                session_id = rec["sessionId"]
            sidechain = bool(rec.get("isSidechain")) or is_sub_file
            agent = "sub" if sidechain else "head"
            msg = rec.get("message") or {}
            content = msg.get("content")

            if rtype == "assistant":
                if model is None and msg.get("model"):
                    model = msg.get("model")
                req = rec.get("requestId") or rec.get("uuid") or ""
                usage = msg.get("usage")
                if usage and req and req not in seen_req:
                    seen_req.add(req)
                    det = usage.get("output_tokens_details") or {}
                    events.append(
                        schema.make_event(
                            "usage", session=session_id, project=project, agent=agent, src=src, ts=_iso(ts),
                            data={
                                "model": msg.get("model"),
                                "input": schema.safe_int(usage.get("input_tokens")),
                                "output": schema.safe_int(usage.get("output_tokens")),
                                "cache_read": schema.safe_int(usage.get("cache_read_input_tokens")),
                                "cache_create": schema.safe_int(usage.get("cache_creation_input_tokens")),
                                "thinking": schema.safe_int(det.get("thinking_tokens")),
                                "effort": rec.get("effort"),
                                "req": req,
                            },
                        )
                    )
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "tool_use":
                            name = str(b.get("name") or "?")
                            tin = b.get("input")
                            pending[str(b.get("id"))] = {
                                "ts": ts, "tool": name, "sig": inc.signature(name, tin),
                                "path": tool_path(tin), "agent": agent, "turn": turn,
                                "in_bytes": in_bytes(tin), "msg": req or _iso(ts),
                            }
                            if agent == "head":
                                turn_calls += 1
                        elif bt == "text" and agent == "head":
                            if req != final_req:
                                final_text_parts = []
                                final_req = req
                            final_text_parts.append(b.get("text") or "")
                continue

            # rtype == "user"
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_result":
                        continue
                    tid = str(b.get("tool_use_id"))
                    call = pending.pop(tid, None)
                    ok = not bool(b.get("is_error"))
                    out_bytes = content_len(b.get("content"))
                    if call is None:
                        call = {"ts": ts, "tool": "?", "sig": "", "path": None, "agent": agent, "turn": turn, "in_bytes": 0, "msg": None}
                    raw_ms = int((ts - call["ts"]).total_seconds() * 1000)
                    data: dict[str, Any] = {
                        "tool": call["tool"], "ok": ok, "ms": raw_ms if raw_ms >= 0 else None,
                        "in_bytes": call["in_bytes"], "out_bytes": out_bytes,
                        "sig": call["sig"], "path": call["path"], "turn": call["turn"], "msg": call.get("msg"),
                    }
                    if tid and tid != "None":
                        data["tool_use_id"] = tid
                    if raw_ms < 0:
                        data["skew_ms"] = raw_ms
                    events.append(
                        schema.make_event("tool.call", session=session_id, project=project, agent=call["agent"], src=src,
                                          ts=_iso(call["ts"]), data=data)
                    )
                    if call["agent"] == "head" and ok:
                        turn_evidence += 1
                continue

            if rec.get("isMeta"):
                continue
            text = _human_text(content)
            if text is None or sidechain:
                continue
            # a new human message = the end of the previous turn
            close_turn(ts)
            turn += 1
            words = _wordset(text)
            repeat_of = prompt_history.check(ts, text, session_id)
            events.append(
                schema.make_event(
                    "prompt", session=session_id, project=project, agent="head", src=src, ts=_iso(ts),
                    data={
                        "chars": len(text), "words": len(words), "turn": turn,
                        "text_hash": hashlib.sha1(text.strip().lower().encode("utf-8", "replace")).hexdigest()[:12],
                        "repeat_of": repeat_of,
                    },
                )
            )

    if first_ts is None or last_ts is None:
        return []
    close_turn(last_ts)
    # unfinished calls: there was no result (the session was cut off or did not wait)
    for tid, call in pending.items():
        events.append(
            schema.make_event(
                "tool.call", session=session_id, project=project, agent=call["agent"], src=src, ts=_iso(call["ts"]),
                data={"tool": call["tool"], "ok": None, "ms": None, "in_bytes": call["in_bytes"], "out_bytes": 0,
                      "sig": call["sig"], "path": call["path"], "turn": call["turn"], "msg": call.get("msg"), "unfinished": True,
                      **({"tool_use_id": tid} if tid and tid != "None" else {})},
            )
        )
    events.append(
        schema.make_event(
            "session.start", session=session_id, project=project, agent="sub" if is_sub_file else "head", src=src,
            ts=_iso(first_ts),
            data={"model": model, "cwd": cwd, "entry": entry, "memory_attached": memory_attached,
                  "transcript": str(path), "bytes": path.stat().st_size},
        )
    )
    events.append(
        schema.make_event(
            "session.stop", session=session_id, project=project, agent="sub" if is_sub_file else "head", src=src,
            ts=_iso(last_ts),
            data={"reason": "eof", "duration_s": int((last_ts - first_ts).total_seconds()), "turns": turn},
        )
    )
    events.sort(key=lambda e: e["ts"])
    return events


def _first_record(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(200):
                line = fh.readline()
                if not line:
                    break
                if not line.startswith("{") or '"timestamp"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("timestamp"):
                    return rec
    except OSError:
        return None
    return None


def iter_project_dirs(projects_dir: Path) -> Iterator[Path]:
    for d in sorted(projects_dir.iterdir()):
        if d.is_dir() and any(d.glob("*.jsonl")):
            yield d


def backfill_project(project_dir: Path, out_dir: Path, *, since: datetime | None = None,
                     suffix: str = "") -> dict[str, Any]:
    files = [p for p in project_dir.rglob("*.jsonl") if "memory" not in p.parts and "tool-results" not in p.parts]
    dated: list[tuple[datetime, Path]] = []
    project = project_dir.name
    cwd_seen = False
    for p in files:
        rec = _first_record(p)
        if rec is None:
            continue
        try:
            t = schema.parse_ts(rec["timestamp"])
        except Exception:  # noqa: BLE001
            continue
        if not cwd_seen and rec.get("cwd"):
            project = _project_name_from_cwd(str(rec.get("cwd")), project_dir.name)
            cwd_seen = True
        if since and t < since:
            continue
        dated.append((t, p))
    dated.sort()
    memory_attached = (project_dir / "memory" / "MEMORY.md").exists()

    out_path = out_dir / f"{project_dir.name}{suffix}.jsonl"
    tmp_path = out_dir / f".{project_dir.name}{suffix}.{os.getpid()}.tmp.jsonl"
    if tmp_path.exists():
        tmp_path.unlink()
    ledger = Ledger(tmp_path)
    history_classes: set[str] = set()
    prompt_history = _PromptHistory()
    stats: Counter[str] = Counter()
    for _, p in dated:
        ev = parse_session(p, project=project, memory_attached=memory_attached, prompt_history=prompt_history)
        if not ev:
            continue
        found = inc.detect_all(ev)
        inc.mark_repeats(found, history_classes)
        history_classes.update(i["data"]["cls"] for i in found)
        ev.extend(found)
        ev.sort(key=lambda e: e["ts"])
        n = ledger.append_many(ev)
        stats["events"] += n
        stats["sessions"] += 1
        stats["incidents"] += len(found)
    lock = tmp_path.with_name(tmp_path.name + ".lock")
    if tmp_path.exists():
        os.replace(tmp_path, out_path)
    else:
        out_path.write_text("", encoding="utf-8")
    if lock.exists():
        try:
            lock.unlink()
        except OSError:
            pass
    return {"project_dir": project_dir.name, "project": project, "ledger": str(out_path),
            "sessions": stats["sessions"], "events": stats["events"], "incidents": stats["incidents"],
            "memory_attached": memory_attached, "files": len(files)}


def backfill_all(projects_dir: Path, out_dir: Path, *, only: list[str] | None = None,
                 days: float | None = None, log=print) -> list[dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    suffix = f"_last{int(days)}d" if days else ""
    results = []
    for d in iter_project_dirs(projects_dir):
        if only and not any(o.lower() in d.name.lower() for o in only):
            continue
        log(f"[backfill] {d.name} ...")
        r = backfill_project(d, out_dir, since=since, suffix=suffix)
        log(f"[backfill] {d.name}: sessions {r['sessions']}, events {r['events']}, incidents {r['incidents']}")
        results.append(r)
    (out_dir / f"_index{suffix}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return results
