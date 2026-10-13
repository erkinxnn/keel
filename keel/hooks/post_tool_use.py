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

State (reads for the blind-edit detector, the window of recent calls) is kept per agent:
the head and each subagent have separate contexts.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import agent_label, emit, known_classes, ledger_for, locked_state, project_name, remember_classes, run_hook, schema  # noqa: E402

from keel import incidents  # noqa: E402
from keel.extract import in_bytes, is_error, norm_path, tool_path, tool_result_len  # noqa: E402

WINDOW = 12
FANOUT_BUCKET_S = 15
MAX_AGENTS = 40
SEEN_TUID = 5000


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
    agent = agent_label(inp)
    with locked_state(cwd, session) as st:
        pend = st.setdefault("pending", {})
        call = pend.pop(key, None)
        if call is None:
            prev_t = float(st.get("last_t") or now)
            call = {"t": prev_t, "tool": tool, "sig": sig, "path": tool_path(tin), "in_bytes": in_bytes(tin),
                    "turn": int(st.get("turn") or 0),
                    "msg": f"{int(st.get('turn') or 0)}:{int(now // FANOUT_BUCKET_S)}", "approx": True}
        st["last_t"] = now
        dur = inp.get("duration_ms")
        data = {"tool": tool, "ok": ok, "in_bytes": call["in_bytes"], "out_bytes": out_bytes,
                "sig": sig, "path": call.get("path"), "turn": call.get("turn"), "msg": call.get("msg")}
        if isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur >= 0:
            data["ms"] = int(dur)
            data["ms_source"] = "hook"
        else:
            data["ms"] = max(int((now - float(call["t"])) * 1000), 0)
            if call.get("approx"):
                data["ms_approx"] = True
        if tuid:
            data["tool_use_id"] = str(tuid)
            seen = st.setdefault("seen_tuid", [])
            seen.append(str(tuid))
            del seen[:-SEEN_TUID]
        if extra:
            data.update(extra)
        ev = emit(schema.make_event("tool.call", session=session, project=project, agent=agent, src="hook", data=data))

        agents = st.setdefault("agents", {})
        per = agents.pop(agent, None) or {}
        agents[agent] = per  # most recently used last
        for old in list(agents)[:-MAX_AGENTS]:
            agents.pop(old, None)
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
                     for rp in reads]
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
        remember_classes(cwd, {f["data"]["cls"] for f in fresh})
    if not fresh:
        return None
    msgs = []
    for f in fresh:
        c = incidents.CLASSES[f["data"]["cls"]]
        rep = " REPEAT of a known class." if f["data"]["repeat"] else ""
        msgs.append(f"KEEL {f['data']['cls']} ({c['name']}): {json.dumps(f['data']['detail'], ensure_ascii=False)}.{rep}")
    return {"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": "\n".join(msgs)}}


def main(inp):
    tool = str(inp.get("tool_name") or "?")
    resp = inp.get("tool_response")
    return record(inp, ok=not is_error(resp), out_bytes=tool_result_len(tool, resp))


if __name__ == "__main__":
    sys.exit(run_hook("post_tool_use", main))
