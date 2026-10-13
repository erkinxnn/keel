# -*- coding: utf-8 -*-
"""SessionStart: record the start of the session and show the agent its scoreboard (<= 20 lines).

Input (Claude Code): {"session_id","transcript_path","cwd","hook_event_name":"SessionStart","source"}
Output: {"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import emit, known_classes, ledger_for, locked_state, memory_attached, project_name, remember_classes, run_hook, schema  # noqa: E402

from keel import incidents, scoreboard, vitals  # noqa: E402
from keel.contract import CONTRACT_EVENTS  # noqa: E402
from keel.ledger import window_since  # noqa: E402


def main(inp):
    cwd = inp.get("cwd")
    session = str(inp.get("session_id") or "nosession")
    led = ledger_for(cwd)
    mem = memory_attached(cwd)
    ev = emit(schema.make_event("session.start", session=session, project=project_name(cwd), src="hook",
                                data={"cwd": cwd, "entry": inp.get("source"), "memory_attached": mem}))
    with locked_state(cwd, session) as st:
        st["session_start_ts"] = ev["ts"]
        st.setdefault("turn", 0)
    lines = []
    if mem is False:
        lines.append("KEEL: project memory is NOT attached to this directory (class MEMORY_LOST).")
        found = incidents.detect_memory_lost([ev])
        if found:
            hist = known_classes(cwd, led)
            for f in found:
                f["data"]["repeat"] = "MEMORY_LOST" in hist
                emit(f)
            remember_classes(cwd, {"MEMORY_LOST"})
    res = vitals.compute(led.iter(since=window_since(14)), contract_events=led.all(ev=set(CONTRACT_EVENTS)))
    lines.append(scoreboard.render_panel(res, title=f"KEEL SCOREBOARD: {project_name(cwd)}", window_label="(14 d)"))
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}


if __name__ == "__main__":
    sys.exit(run_hook("session_start", main))
