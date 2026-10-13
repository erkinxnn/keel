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
The events are written inside the state lock and only then marked as seen, so a failed write
does not hide a call from a later import. keel.vitals also counts tool.call rows with the same
tool_use_id once, preferring a finished row over an unfinished one (defence in depth).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import emit, flush_call, load_state, locked_state, log, memory_attached, project_name, run_hook  # noqa: E402

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
    if marker in (load_state(cwd, session).get("sub_done") or []):
        return None  # cheap check without the lock; repeated under the lock below
    events = parse_session(Path(p), project=project_name(cwd), memory_attached=memory_attached(cwd), prompt_history=_PromptHistory())
    agent_id = str(inp.get("agent_id") or os.path.splitext(os.path.basename(p))[0])
    with locked_state(cwd, session) as st:
        done = st.setdefault("sub_done", [])
        if marker in done:
            return None
        seen_t = st.setdefault("seen_tuid", [])
        seen_r = st.setdefault("sub_seen_req", [])
        once = st.setdefault("sub_once", [])
        seen_t_set, seen_r_set, once_set = set(seen_t), set(seen_r), set(once)
        new_t: list[str] = []
        new_r: list[str] = []
        new_once: list[str] = []
        fresh = []
        for e in events:
            d = e["data"]
            if e["ev"] == "tool.call" and d.get("tool_use_id"):
                tu = str(d["tool_use_id"])
                if tu in seen_t_set:
                    continue
                if d.get("ok") is not None:
                    seen_t_set.add(tu)
                    new_t.append(tu)
            elif e["ev"] == "usage" and d.get("req"):
                rq = str(d["req"])
                if rq in seen_r_set:
                    continue
                seen_r_set.add(rq)
                new_r.append(rq)
            elif e["ev"] in ("session.start", "session.stop"):
                k = f"{agent_id}:{e['ev']}"
                if k in once_set:
                    continue
                once_set.add(k)
                new_once.append(k)
            fresh.append(e)
        for e in fresh:
            e["session"] = session
            e["agent"] = f"sub:{agent_id}"
            e["src"] = "hook"
            emit(e)
        # written first, marked after: if the write fails, the state is not saved and a later
        # SubagentStop (or the live hook) still records these calls
        flush_call(cwd)
        done.append(marker)
        seen_t.extend(new_t)
        seen_r.extend(new_r)
        once.extend(new_once)
        del done[:-200]
        del seen_t[:-5000]
        del seen_r[:-5000]
        del once[:-400]
    return None


if __name__ == "__main__":
    sys.exit(run_hook("subagent_stop", main))
