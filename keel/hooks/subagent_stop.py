# -*- coding: utf-8 -*-
"""SubagentStop: the subagent's usage and calls go into the ledger with the same ruler as history.

Input: {"session_id","cwd","hook_event_name":"SubagentStop","agent_id","agent_type",
       "agent_transcript_path"? , "transcript_path", "last_assistant_message"?}
The subagent transcript is parsed by the standard keel.backfill.parse_session (the agent is
marked "sub", the session is the parent's). The same transcript (path + size) is not parsed a
second time. If the transcript GREW between two calls it is parsed again in full, so earlier
usage rows are written twice; every usage row carries the model request id (`req`) and
keel.vitals counts rows with the same id once.
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
    for e in events:
        e["session"] = session
        e["agent"] = f"sub:{agent_id}"
        e["src"] = "hook"
    for e in events:
        emit(e)
    return None


if __name__ == "__main__":
    sys.exit(run_hook("subagent_stop", main))
