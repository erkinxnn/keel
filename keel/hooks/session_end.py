# -*- coding: utf-8 -*-
"""SessionEnd: record the end of the session with a duration taken from the session state (without
reading the whole ledger: the SessionEnd budget in Claude Code is 1.5 s unless the hook sets a
longer timeout).

Input: {"session_id","cwd","hook_event_name":"SessionEnd","reason"}
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import emit, load_state, project_name, run_hook, schema  # noqa: E402


def main(inp):
    cwd = inp.get("cwd")
    session = str(inp.get("session_id") or "nosession")
    st = load_state(cwd, session)
    start = st.get("session_start_ts")
    dur = None
    if start:
        try:
            dur = int((schema.parse_ts(schema.now_iso()) - schema.parse_ts(start)).total_seconds())
        except Exception:  # noqa: BLE001
            dur = None
    emit(schema.make_event("session.stop", session=session, project=project_name(cwd), src="hook",
                           data={"reason": inp.get("reason"), "duration_s": dur, "turns": int(st.get("turn") or 0)}))
    return None


if __name__ == "__main__":
    sys.exit(run_hook("session_end", main))
