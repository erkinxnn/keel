# -*- coding: utf-8 -*-
"""UserPromptSubmit: record a prompt (only numbers and a hash) and whether it repeats an order.

The words of the message are kept ONLY as a set of words in the project state
`<keel dir>/state/prompts.jsonl` for the Jaccard comparison; the text does not go into the ledger.
Harness plumbing (summaries after compaction, interruptions) does not count as a human message.
Resets the turn's Stop-block counter.
Input: {"session_id","cwd","hook_event_name":"UserPromptSubmit","prompt"}
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import emit, keel_dir, known_classes, ledger_for, locked_state, project_name, remember_classes, run_hook, schema  # noqa: E402

from keel import incidents  # noqa: E402
from keel._fast import sha256_hex  # noqa: E402
from keel.extract import is_human_prompt  # noqa: E402

_WORD = re.compile(r"[\w\-]{3,}", re.UNICODE)
DAYS = 14
JACC = 0.8
MIN_WORDS = 6


def main(inp):
    cwd = inp.get("cwd")
    session = str(inp.get("session_id") or "nosession")
    text = str(inp.get("prompt") or "")
    if not is_human_prompt(text):
        return None
    t = text.strip()
    words = sorted({w.lower() for w in _WORD.findall(t)})
    h = sha256_hex(t.lower().encode("utf-8", "replace"))[:12]
    hist_dir = os.path.join(keel_dir(cwd), "state")
    os.makedirs(hist_dir, exist_ok=True)
    hist_path = os.path.join(hist_dir, "prompts.jsonl")
    now = time.time()
    repeat_of = None
    if len(words) >= MIN_WORDS and os.path.exists(hist_path):
        ws = set(words)
        with open(hist_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if now - float(o.get("t", 0)) > DAYS * 86400:
                    continue
                if o.get("h") == h:
                    repeat_of = o["h"]
                    break
                pw = set(o.get("w", []))
                u = len(ws | pw)
                if u and len(ws & pw) / u >= JACC:
                    repeat_of = o["h"]
                    break
    line = (json.dumps({"t": now, "h": h, "w": words, "s": session}, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(hist_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    with locked_state(cwd, session) as st:
        st["turn"] = int(st.get("turn") or 0) + 1
        st["stop_blocks_turn"] = 0
        st["last_t"] = now
        turn = st["turn"]
    led = ledger_for(cwd)
    project = project_name(cwd)
    hist = known_classes(cwd, led) if repeat_of else set()
    if repeat_of:
        remember_classes(cwd, {"REPEAT_ORDER"})
    emit(schema.make_event("prompt", session=session, project=project, src="hook",
                           data={"chars": len(t), "words": len(words), "turn": turn, "text_hash": h, "repeat_of": repeat_of}))
    if repeat_of:
        emit(schema.make_event("incident", session=session, project=project, src="hook",
                               data={"cls": "REPEAT_ORDER", "severity": incidents.CLASSES["REPEAT_ORDER"]["severity"],
                                     "repeat": "REPEAT_ORDER" in hist, "detail": {"repeat_of": repeat_of, "words": len(words)}}))
        return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": "KEEL REPEAT_ORDER: the user is repeating an instruction that was already given. "
                                                            "That means last time it was not carried out or not recorded in a durable place. "
                                                            "First find out what was done about it, then answer."}}
    return None


if __name__ == "__main__":
    sys.exit(run_hook("user_prompt_submit", main))
