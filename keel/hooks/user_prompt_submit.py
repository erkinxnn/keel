# -*- coding: utf-8 -*-
"""UserPromptSubmit: record a prompt (only numbers and a hash) and whether it repeats an order.

The words of the message are kept ONLY as a set of words in the project state
`<keel dir>/state/prompts.jsonl` for the Jaccard comparison; the text does not go into the ledger.
Every write rewrites that file under a lock without the entries older than DAYS (14) days, so a
word set is deleted at the first prompt after it expires (it stays longer only if no prompt comes).
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
from keel.ledger import _FileLock  # noqa: E402
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
    ws = set(words)
    kept: list[str] = []
    with _FileLock(hist_path + ".lock"):
        try:
            with open(hist_path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            lines = []
        for line in lines:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict) or now - schema.safe_float(o.get("t")) > DAYS * 86400:
                continue  # older than DAYS: deleted from the file, not only skipped
            kept.append(line)
            if repeat_of is not None or len(words) < MIN_WORDS:
                continue
            if o.get("h") == h:
                repeat_of = o["h"]
                continue
            pw = set(o.get("w") or [])
            u = len(ws | pw)
            if u and len(ws & pw) / u >= JACC:
                repeat_of = o.get("h")
        kept.append(json.dumps({"t": now, "h": h, "w": words, "s": session}, ensure_ascii=False))
        tmp = f"{hist_path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write("".join(x + chr(10) for x in kept))
        os.replace(tmp, hist_path)
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
