# -*- coding: utf-8 -*-
"""PostToolUse: write a tool.call, run the detectors, and on a new incident tell the agent in one
line through additionalContext. Advises, does not block.

Input: {"session_id","cwd","hook_event_name":"PostToolUse","tool_name","tool_input","tool_response",
        "tool_use_id","duration_ms"?,"agent_id"?}
Output (only on an incident): {"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"..."}}
The field ruler (out_bytes, ok, in_bytes) is shared with backfill: keel.extract. `out_bytes` is
what the result adds to the MODEL'S context (edit payloads and images are normalised there).

Claude Code fires PostToolUse only for tool calls that succeed; failed calls go to
PostToolUseFailure, handled by post_tool_use_failure.py through `record()` below.

Duration: `duration_ms` from the hook input when present (tool execution only, the documented
field excludes permission prompts); otherwise the PreToolUse timestamp if that optional adapter
is registered; otherwise the gap since the previous event of the session, marked `ms_approx`
(it includes model thinking and other calls, good enough only for a stall over 30 minutes).

State (reads for the blind-edit detector, the window of recent calls, the fan-out cluster) is kept
per agent: the head and each subagent have separate contexts. The tool.call is written to the ledger
inside the state lock and only then its tool_use_id is marked as seen; a call whose id is already
seen (imported by SubagentStop first) is not written a second time.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import agent_label, emit, flush_call, known_classes, ledger_for, locked_state, project_name, remember_classes, run_hook, schema  # noqa: E402

from keel import incidents  # noqa: E402
from keel.extract import in_bytes, is_error, norm_path, tool_path, tool_result_len  # noqa: E402

WINDOW = 12
FANOUT_GAP_S = 15
MAX_AGENTS = 40
SEEN_TUID = 5000


def _agent_state(st: dict, agent: str) -> dict:
    """Per-agent state (reads, recent calls, fan-out cluster), most recently used last.

    State written by 0.1.0 kept one `read_paths`/`recent` for the whole session: it is migrated on
    first use; its reads stay valid for every agent of the session (`legacy_reads`), its recent
    calls go to the head. The cap evicts the least recently used SUBAGENT; the head is never evicted."""
    agents = st.setdefault("agents", {})
    if "read_paths" in st or "recent" in st:
        old_reads = st.pop("read_paths", None) or []
        old_recent = st.pop("recent", None) or []
        st["legacy_reads"] = [str(p) for p in old_reads][-500:]
        head = agents.setdefault("head", {})
        head["recent"] = (list(old_recent) + list(head.get("recent") or []))[-WINDOW:]
    per = agents.pop(agent, None) or {}
    agents[agent] = per
    subs = [k for k in agents if k != "head"]
    for old in subs[:max(len(subs) - (MAX_AGENTS - 1), 0)]:
        agents.pop(old, None)
    return per


def _fanout_msg(per: dict, turn: int, now: float) -> str:
    """Live stand-in for "one model message" (the hook input carries no message id): Agent calls of
    one agent in one turn, each completed within FANOUT_GAP_S seconds of the previous one, share a
    key. A gap rule, not a fixed clock bucket, so a burst is not split at a bucket boundary; calls of
    two messages less than 15 s apart are merged."""
    fan = per.get("fan") or {}
    if fan.get("msg") and fan.get("turn") == turn and 0 <= now - float(fan.get("t") or 0) <= FANOUT_GAP_S:
        msg = str(fan["msg"])
    else:
        msg = f"{turn}:{int(now)}"
    per["fan"] = {"turn": turn, "t": now, "msg": msg}
    return msg


def record(inp: dict, ok: bool, out_bytes: int, extra: dict | None = None, event_name: str = "PostToolUse"):
    cwd = inp.get("cwd")
    session = str(inp.get("session_id") or "nosession")
    tool = str(inp.get("tool_name") or "?")
    tin = inp.get("tool_input") or {}
    sig = incidents.signature(tool, tin)
    tuid = inp.get("tool_use_id")
    key = str(tuid or sig)
    now = time.time()
    led = ledger_for(cwd)
    project = project_name(cwd)
    fresh = []
    out = None
    agent = agent_label(inp)
    with locked_state(cwd, session) as st:
        pend = st.setdefault("pending", {})
        call = pend.pop(key, None)
        seen = st.setdefault("seen_tuid", [])
        if tuid and str(tuid) in seen:
            st["last_t"] = now
            return None  # already in the ledger: SubagentStop imported it from the transcript first
        if call is None:
            prev_t = float(st.get("last_t") or now)
            call = {"t": prev_t, "tool": tool, "sig": sig, "path": tool_path(tin), "in_bytes": in_bytes(tin),
                    "turn": int(st.get("turn") or 0), "approx": True}
        st["last_t"] = now
        per = _agent_state(st, agent)
        msg = _fanout_msg(per, int(call.get("turn") or 0), now) if tool == "Agent" else call.get("msg")
        dur = inp.get("duration_ms")
        data = {"tool": tool, "ok": ok, "in_bytes": call["in_bytes"], "out_bytes": out_bytes,
                "sig": sig, "path": call.get("path"), "turn": call.get("turn"), "msg": msg}
        if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur >= 0:
            data["ms"] = int(dur)
            data["ms_source"] = "hook"
        else:
            data["ms"] = max(int((now - float(call["t"])) * 1000), 0)
            if call.get("approx"):
                data["ms_approx"] = True
        if tuid:
            data["tool_use_id"] = str(tuid)
        if extra:
            data.update(extra)
        ev = emit(schema.make_event("tool.call", session=session, project=project, agent=agent, src="hook", data=data))

        # read and written paths for the blind-edit detector, per agent context
        reads = per.setdefault("read_paths", [])
        p = call.get("path")
        if ok and p and tool in ("Read", "NotebookRead") and out_bytes >= incidents.MIN_READ_CHARS:
            np_ = norm_path(p)
            if np_ not in reads:
                reads.append(np_)
        if ok and p and tool == "Write":
            np_ = norm_path(p)
            if np_ not in reads:
                reads.append(np_)
        del reads[:-500]

        recent = per.setdefault("recent", [])
        recent.append({"ev": "tool.call", "ts": ev["ts"], "session": session, "project": project, "agent": agent,
                       "src": "hook", "data": ev["data"]})
        del recent[:-WINDOW]
        synthetic = [{"ev": "tool.call", "ts": ev["ts"], "session": session, "project": project, "agent": agent,
                      "src": "hook", "data": {"tool": "Read", "path": rp, "sig": "", "ok": True, "out_bytes": incidents.MIN_READ_CHARS}}
                     for rp in list(st.get("legacy_reads") or []) + reads]
        found = []
        for det in (incidents.detect_loops, incidents.detect_floods, incidents.detect_stalls,
                    incidents.detect_error_bursts, incidents.detect_fanout):
            found.extend(det(recent))
        if ok:
            found.extend(incidents.detect_blind_edits(synthetic + recent[-1:]))

        said = st.setdefault("said", [])
        hist = known_classes(cwd, led) if found else set()
        for f in found:
            k = agent + "|" + incidents.incident_key(f)
            if k in said:
                continue
            said.append(k)
            f["data"]["repeat"] = f["data"]["cls"] in hist
            emit(f)
            fresh.append(f)
            hist.add(f["data"]["cls"])
        del said[:-300]
        if fresh:
            msgs = []
            for f in fresh:
                c = incidents.CLASSES[f["data"]["cls"]]
                rep = " REPEAT of a known class." if f["data"]["repeat"] else ""
                msgs.append(f"KEEL {f['data']['cls']} ({c['name']}): {json.dumps(f['data']['detail'], ensure_ascii=False)}.{rep}")
            out = {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": "\n".join(msgs)}}
        # the tool.call is written BEFORE its id is marked as seen: if the write fails, the state is
        # not saved (the exception leaves the with-block) and SubagentStop can still import the call
        flush_call(cwd, out)
        if tuid:
            seen.append(str(tuid))
            del seen[:-SEEN_TUID]
    if fresh:
        remember_classes(cwd, {f["data"]["cls"] for f in fresh})
    return out


def main(inp):
    tool = str(inp.get("tool_name") or "?")
    resp = inp.get("tool_response")
    return record(inp, ok=not is_error(resp), out_bytes=tool_result_len(tool, resp))


if __name__ == "__main__":
    sys.exit(run_hook("post_tool_use", main))
