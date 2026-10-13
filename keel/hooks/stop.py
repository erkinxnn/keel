# -*- coding: utf-8 -*-
"""Stop (end of a turn): write the usage of the last turn (de-duplicated by requestId through the
session state), a claim with its evidence count and negation-cancelled count, and on a "done"
without evidence either say so in the log (default) or block the reply (KEEL_STRICT=1), at most
once per turn.

Input: {"session_id","transcript_path","cwd","hook_event_name":"Stop","last_assistant_message"?}
The text of the last reply is taken from `last_assistant_message` (Claude Code hook docs); the
transcript is needed only for usage and for counting the turn's evidence.
Output in strict mode on a violation: decision/reason both at the top level and inside
hookSpecificOutput. The top-level form is the documented one; the nested form is kept from an
earlier edition of the docs and is not in the current reference (see docs/ARCHITECTURE.md, section 8).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import emit, flush_call, known_classes, ledger_for, locked_state, log, project_name, remember_classes, run_hook, schema  # noqa: E402

from keel import incidents  # noqa: E402
from keel._fast import sha256_hex  # noqa: E402
from keel.extract import is_human_prompt  # noqa: E402


def _tail_turn(transcript: str):
    """Lines of the last turn: from the last human message to the end of the file."""
    turn: list[dict] = []
    with open(transcript, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") not in ("user", "assistant") or rec.get("isSidechain"):
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            is_tool_result = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            if rec["type"] == "user" and not is_tool_result and not rec.get("isMeta"):
                text = content if isinstance(content, str) else ""
                if isinstance(content, list):
                    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
                if is_human_prompt(text):
                    turn = []
            turn.append(rec)
    return turn


def main(inp):
    cwd = inp.get("cwd")
    session = str(inp.get("session_id") or "nosession")
    tp = inp.get("transcript_path")
    led = ledger_for(cwd)
    project = project_name(cwd)
    turn = _tail_turn(tp) if tp and os.path.exists(tp) else []
    if not turn:
        log(cwd, "stop: transcript unavailable, usage and turn evidence not counted")
    calls = 0
    evidence = 0
    final_text: list[str] = []
    final_req = None
    out: dict | None = None
    found: list = []
    with locked_state(cwd, session) as st:
        seen = list(st.get("seen_req", []))
        seen_set = set(seen)
        new_seen: list[str] = []
        for rec in turn:
            msg = rec.get("message") or {}
            content = msg.get("content")
            if rec["type"] == "assistant":
                req = rec.get("requestId") or rec.get("uuid")
                usage = msg.get("usage")
                if usage and req and req not in seen_set:
                    seen_set.add(req)
                    new_seen.append(req)
                    det = usage.get("output_tokens_details") or {}
                    emit(schema.make_event("usage", session=session, project=project, src="hook", ts=rec.get("timestamp"),
                                           data={"model": msg.get("model"), "input": schema.safe_int(usage.get("input_tokens")),
                                                 "output": schema.safe_int(usage.get("output_tokens")),
                                                 "cache_read": schema.safe_int(usage.get("cache_read_input_tokens")),
                                                 "cache_create": schema.safe_int(usage.get("cache_creation_input_tokens")),
                                                 "thinking": schema.safe_int(det.get("thinking_tokens")), "effort": rec.get("effort"),
                                                 "req": str(req)}))
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use":
                            calls += 1
                        elif b.get("type") == "text":
                            if req != final_req:
                                final_text = []
                                final_req = req
                            final_text.append(b.get("text") or "")
            else:
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and not b.get("is_error"):
                            evidence += 1
        lam = inp.get("last_assistant_message")
        text = lam if isinstance(lam, str) and lam.strip() else "\n".join(final_text)
        words, negated = incidents.claim_scan(text)
        blocks = int(st.get("stop_blocks_turn") or 0)
        strict = False
        if words or negated:
            claim = schema.make_event("claim", session=session, project=project, src="hook",
                                      data={"kind": "done" if words else "negated", "words": sorted(set(words))[:6], "negated_n": negated,
                                            "evidence_n": evidence, "tool_calls_in_turn": calls, "turn": int(st.get("turn") or 0),
                                            "text_hash": sha256_hex(text.encode("utf-8", "replace"))[:12], "chars": len(text)})
            emit(claim)
            found = incidents.detect_unverified_claims([claim])
            if found:
                hist = known_classes(cwd, led)
                for f in found:
                    f["data"]["repeat"] = "UNVERIFIED_CLAIM" in hist
                    emit(f)
                strict = os.environ.get("KEEL_STRICT") == "1" and blocks < 1 and not inp.get("stop_hook_active")
                why = "zero tool calls in this turn" if calls == 0 else f"{calls} tool call(s) in this turn and none of them succeeded"
                reason = ("KEEL UNVERIFIED_CLAIM: the reply contains completion words (" + ", ".join(sorted(set(words))[:4]) +
                          ") with " + why + ". Attach evidence (the command and its output, a file path) "
                          "or replace \"done\" with \"not verified\".")
                if strict:
                    out = {"decision": "block", "reason": reason,
                           "hookSpecificOutput": {"hookEventName": "Stop", "decision": "block", "reason": reason}}
                else:
                    log(cwd, "stop: " + reason)
        # the events are written BEFORE the requests are marked as seen: if the write fails, flush_call raises,
        # the state is not saved (the exception leaves the with-block) and the next Stop imports the usage again
        flush_call(cwd, out)
        st["seen_req"] = (seen + new_seen)[-5000:]
        if strict:
            st["stop_blocks_turn"] = blocks + 1
    if found:
        remember_classes(cwd, {"UNVERIFIED_CLAIM"})
    return out


if __name__ == "__main__":
    sys.exit(run_hook("stop", main))
