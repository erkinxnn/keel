# -*- coding: utf-8 -*-
"""PreToolUse: remember the moment of the call (for its duration) and the model message (for fan-out).

Blocks nothing. The only output is empty (exit code 0). The read path is recorded in
PostToolUse, when the size of the result is known (a read shorter than 100 chars does not count as a read).
Input: {"session_id","cwd","hook_event_name":"PreToolUse","tool_name","tool_input","tool_use_id"?}
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import locked_state, run_hook  # noqa: E402

from keel import incidents  # noqa: E402
from keel.extract import in_bytes, tool_path  # noqa: E402

FANOUT_BUCKET_S = 15


def main(inp):
    cwd = inp.get("cwd")
    session = str(inp.get("session_id") or "nosession")
    tool = str(inp.get("tool_name") or "?")
    tin = inp.get("tool_input") or {}
    sig = incidents.signature(tool, tin)
    key = str(inp.get("tool_use_id") or sig)
    now = time.time()
    with locked_state(cwd, session) as st:
        pend = st.setdefault("pending", {})
        pend[key] = {"t": now, "tool": tool, "sig": sig, "path": tool_path(tin), "in_bytes": in_bytes(tin),
                     "turn": int(st.get("turn") or 0), "msg": f"{int(st.get('turn') or 0)}:{int(now // FANOUT_BUCKET_S)}"}
        if len(pend) > 200:  # guard against a leak from interrupted calls
            for k in list(pend)[:-200]:
                pend.pop(k, None)
    return None


if __name__ == "__main__":
    sys.exit(run_hook("pre_tool_use", main))
