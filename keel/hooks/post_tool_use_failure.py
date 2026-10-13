# -*- coding: utf-8 -*-
"""PostToolUseFailure: a tool call that failed (or was interrupted) goes into the ledger as a
tool.call with ok=false, through the same `record()` as PostToolUse, so LOOP-with-failure and
TOOL_ERROR_BURST see live failures.

Input (Claude Code hooks reference): {"session_id","cwd","hook_event_name":"PostToolUseFailure",
"tool_name","tool_input","tool_use_id","error","is_interrupt"?,"duration_ms"?,"agent_id"?}
Stored: ok=false, out_bytes = length of the error text (it enters the model's context),
`interrupted: true` when is_interrupt is set (V6 does not count interrupts as errors).
The error text itself is NOT stored.
Output (only on an incident): {"hookSpecificOutput":{"hookEventName":"PostToolUseFailure","additionalContext":"..."}}
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import run_hook  # noqa: E402
from post_tool_use import record  # noqa: E402


def main(inp):
    err = inp.get("error")
    if isinstance(err, str):
        n = len(err)
    elif err is None:
        n = 0
    else:
        n = len(json.dumps(err, ensure_ascii=False))
    extra = {"failure": True}
    if inp.get("is_interrupt"):
        extra["interrupted"] = True
    return record(inp, ok=False, out_bytes=n, extra=extra, event_name="PostToolUseFailure")


if __name__ == "__main__":
    sys.exit(run_hook("post_tool_use_failure", main))
