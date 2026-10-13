# -*- coding: utf-8 -*-
"""SubagentStop: the subagent's usage and calls go into the ledger with the same ruler as history.

Input: {"session_id","cwd","hook_event_name":"SubagentStop","agent_id","agent_type",
       "agent_transcript_path"? , "transcript_path", "last_assistant_message"?}
The subagent transcript is parsed by the standard keel.backfill.parse_session (the agent is
marked "sub", the session is the parent's). The same transcript (path + size) is not parsed a
second time. If the transcript GREW between two calls it is parsed again in full; only events not
seen before are written:
    * tool.call: skipped when its `tool_use_id` was already recorded, either live by
      PostToolUse/PostToolUseFailure inside the subagent (those hooks fire inside subagents too)
      or by an earlier parse; finished calls only are marked as seen;
    * usage: skipped when its request id (`req`) was imported before;
    * session.start / session.stop of the subagent: written once per agent.
keel.vitals also counts tool.call rows with the same tool_use_id once (defence in depth).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import emit, locked_state, log, memory_attached, project_name, run_hook  # noqa: E402

from keel.backfill import _PromptHistory, parse_session  # noqa: E402

# backfill (and only it) works with pathlib: it is not on the hot path, a subagent ends rarely


def main(inp):
    cwd = inp.get("cwd")
    session = str(inp.get("session_id") or "nosession")
    tp = inp.get("agent_transcript_path") or inp.get("transcript_path")
    if not tp or not os.path.exists(tp):
        log(cwd, "subagent_stop: subagent transcript not found")
        return None
    p = tp
    marker = f"{p}:{os.path.getsize(p)}"
    with locked_state(cwd, session) as st:
        done = st.setdefault("sub_done", [])
        if marker in done:
            return None
        done.append(marker)
        del done[:-200]
    events = parse_session(Path(p), project=project_name(cwd), memory_attached=memory_attached(cwd), prompt_history=_PromptHistory())
    agent_id = str(inp.get("agent_id") or os.path.splitext(os.path.basename(p))[0])
    fresh = []
    with locked_state(cwd, session) as st:
        seen_t = st.setdefault("seen_tuid", [])
        seen_t_set = set(seen_t)
        seen_r = st.setdefault("sub_seen_req", [])
        seen_r_set = set(seen_r)
        once = st.setdefault("sub_once", [])
        for e in events:
            d = e["data"]
            if e["ev"] == "tool.call" and d.get("tool_use_id"):
                tu = str(d["tool_use_id"])
                if tu in seen_t_set:
                    continue
                if d.get("ok") is not None:
                    seen_t_set.add(tu)
                    seen_t.append(tu)
            elif e["ev"] == "usage" and d.get("req"):
                rq = str(d["req"])
                if rq in seen_r_set:
                    continue
                seen_r_set.add(rq)
                seen_r.append(rq)
            elif e["ev"] in ("session.start", "session.stop"):
                k = f"{agent_id}:{e['ev']}"
                if k in once:
                    continue
                once.append(k)
            fresh.append(e)
        del seen_t[:-5000]
        del seen_r[:-5000]
        del once[:-400]
    for e in fresh:
        e["session"] = session
        e["agent"] = f"sub:{agent_id}"
        e["src"] = "hook"
        emit(e)
    return None


if __name__ == "__main__":
    sys.exit(run_hook("subagent_stop", main))
