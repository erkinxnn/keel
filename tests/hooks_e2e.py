# -*- coding: utf-8 -*-
"""End-to-end probe of the hook adapters on synthetic Claude Code input.

Run: python tests/hooks_e2e.py  -> prints the latency of each adapter (V13) and checks that the
ledger received the expected events and that a "done without evidence" was caught. Installs
nothing into settings.json: the adapters are called directly, the way Claude Code would call them.
There is no check of the form `or True`: every check can fail.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "keel" / "hooks"
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from keel.ledger import Ledger  # noqa: E402

sys.path.insert(0, str(HOOKS))
from _common import encode_project_dir  # noqa: E402

KEEL_HOME = tempfile.mkdtemp(prefix="keel_home_")


def call(hook: str, payload: dict, env: dict | None = None) -> tuple[int, str, float]:
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, str(HOOKS / hook)], input=json.dumps(payload, ensure_ascii=False),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "KEEL_HOME": KEEL_HOME, **(env or {})}, timeout=60)
    return r.returncode, r.stdout.strip(), (time.perf_counter() - t0) * 1000


def led_of(cwd: str) -> Ledger:
    return Ledger(Path(KEEL_HOME) / encode_project_dir(cwd) / "ledger.jsonl")


def main() -> int:
    checks = []
    with tempfile.TemporaryDirectory() as td:
        cwd = str(Path(td).resolve())
        sid = "e2e-session"
        base = {"session_id": sid, "cwd": cwd}
        tr = Path(td) / "t.jsonl"
        lines = [
            {"type": "user", "timestamp": "2026-09-02T10:00:00Z", "sessionId": sid, "message": {"role": "user", "content": "do it"}},
            {"type": "assistant", "timestamp": "2026-09-02T10:00:03Z", "sessionId": sid, "requestId": "r1",
             "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 100, "output_tokens": 20},
                         "content": [{"type": "text", "text": "Done, everything is finished."}]}},
        ]
        tr.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")

        lat = {}
        rc, out, ms = call("session_start.py", {**base, "hook_event_name": "SessionStart", "source": "startup"}); lat["session_start"] = ms
        checks.append(("session_start rc0", rc == 0))
        checks.append(("session_start panel injected", "additionalContext" in out and "SCOREBOARD" in out))
        checks.append(("session_start no-data on empty", "NO DATA" in out))

        prompt = "build the sales report for August and send it as a file"
        rc, out, ms = call("user_prompt_submit.py", {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt}); lat["user_prompt_submit"] = ms
        checks.append(("prompt rc0 silent", rc == 0 and out == ""))
        rc, out, ms = call("user_prompt_submit.py", {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt})
        checks.append(("repeat order detected", "REPEAT_ORDER" in out))
        rc, out, ms = call("user_prompt_submit.py", {**base, "hook_event_name": "UserPromptSubmit", "prompt": "This session is being continued from a previous conversation. Summary of the conversation"})
        checks.append(("harness summary ignored", out == ""))

        # Edit without Read -> BLIND_EDIT
        rc, out, ms = call("pre_tool_use.py", {**base, "hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "C:/x/a.py", "old_string": "a", "new_string": "b"}, "tool_use_id": "t1"}); lat["pre_tool_use"] = ms
        checks.append(("pre rc0 silent", rc == 0 and out == ""))
        rc, out, ms = call("post_tool_use.py", {**base, "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": "C:/x/a.py", "old_string": "a", "new_string": "b"}, "tool_response": {"ok": True}, "tool_use_id": "t1"}); lat["post_tool_use"] = ms
        checks.append(("blind edit advised", "BLIND_EDIT" in out))
        # Read of >= 100 chars, then Edit of the same file -> silence
        call("pre_tool_use.py", {**base, "hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "C:/x/b.py"}, "tool_use_id": "r1"})
        call("post_tool_use.py", {**base, "hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": "C:/x/b.py"}, "tool_response": {"type": "text", "file": {"filePath": "C:/x/b.py", "content": "x" * 300}}, "tool_use_id": "r1"})
        call("pre_tool_use.py", {**base, "hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "C:\\x\\b.py", "old_string": "a", "new_string": "b"}, "tool_use_id": "t2"})
        rc, out, ms = call("post_tool_use.py", {**base, "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": "C:\\x\\b.py", "old_string": "a", "new_string": "b"}, "tool_response": {"ok": True}, "tool_use_id": "t2"})
        checks.append(("edit after real read silent", "BLIND_EDIT" not in out))
        # four identical failing Bash calls, routed the way Claude Code routes failures: to
        # PostToolUseFailure (fields error, is_interrupt, duration_ms). LOOP is reported exactly once, on the third
        outs = []
        for i in range(4):
            call("pre_tool_use.py", {**base, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "pytest -q"}, "tool_use_id": f"b{i}"})
            rc, o, ms = call("post_tool_use_failure.py", {**base, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "pytest  -q"}, "error": "Exit code 1: 1 failed", "is_interrupt": False, "duration_ms": 1234, "tool_use_id": f"b{i}"})
            lat["post_tool_use_failure"] = ms
            outs.append(o)
        fails = [e for e in led_of(cwd).iter(ev="tool.call") if e["data"].get("tool_use_id", "").startswith("b")]
        checks.append(("R9 PostToolUseFailure records ok=false with duration_ms", len(fails) == 4 and all(e["data"]["ok"] is False and e["data"]["ms"] == 1234 for e in fails)))
        checks.append(("R9 the error text is not stored", not any("1 failed" in json.dumps(e) for e in led_of(cwd).iter())))
        call("post_tool_use_failure.py", {**base, "hook_event_name": "PostToolUseFailure", "tool_name": "Read", "tool_input": {"file_path": "C:/x/z.py"}, "error": "interrupted", "is_interrupt": True, "tool_use_id": "int1"})
        intr = [e for e in led_of(cwd).iter(ev="tool.call") if e["data"].get("tool_use_id") == "int1"]
        checks.append(("R9 an interrupt is recorded as interrupted", bool(intr) and intr[0]["data"].get("interrupted") is True))
        checks.append(("loop silent before 3rd", "LOOP" not in outs[0] and "LOOP" not in outs[1]))
        checks.append(("loop advised exactly on 3rd", "LOOP" in outs[2]))
        checks.append(("loop not repeated on 4th", "LOOP" not in outs[3]))
        led = Ledger(Path(KEEL_HOME) / encode_project_dir(cwd) / "ledger.jsonl")
        checks.append(("ledger lives outside the project", led.exists() and not (Path(cwd) / ".keel").exists()))
        incs = {e["data"]["cls"] for e in led.iter(ev="incident")}
        checks.append(("ledger has LOOP+BLIND_EDIT+ERROR_BURST", {"LOOP", "BLIND_EDIT", "TOOL_ERROR_BURST"} <= incs))
        # a string response with an error counts as an error (same ruler as history)
        call("pre_tool_use.py", {**base, "hook_event_name": "PreToolUse", "tool_name": "Grep", "tool_input": {"pattern": "x"}, "tool_use_id": "g1"})
        call("post_tool_use.py", {**base, "hook_event_name": "PostToolUse", "tool_name": "Grep", "tool_input": {"pattern": "x"}, "tool_response": "Error: pattern invalid", "tool_use_id": "g1"})
        grep_calls = [e for e in led.iter(ev="tool.call") if e["data"]["tool"] == "Grep"]
        checks.append(("string error response is error", bool(grep_calls) and grep_calls[-1]["data"]["ok"] is False))

        # the flood metric measures what enters the MODEL context, not the hook payload
        big_edit_resp = {"filePath": "C:/x/c.py", "oldString": "a", "newString": "b", "originalFile": "x" * 60_000,
                         "structuredPatch": [{"lines": ["+" + "y" * 5_000]}], "userModified": False, "replaceAll": False}
        call("pre_tool_use.py", {**base, "hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"file_path": "C:/x/c.py", "old_string": "a", "new_string": "b"}, "tool_use_id": "e1"})
        rc, out, ms = call("post_tool_use.py", {**base, "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": "C:/x/c.py", "old_string": "a", "new_string": "b"}, "tool_response": big_edit_resp, "tool_use_id": "e1"})
        edit_c = [e for e in led.iter(ev="tool.call") if e["data"].get("path") == "C:/x/c.py"]
        checks.append(("big Edit payload counted as a short confirmation", bool(edit_c) and edit_c[-1]["data"]["out_bytes"] == 200))
        checks.append(("big Edit payload is not a FLOOD", "FLOOD" not in out))
        image_resp = {"type": "image", "file": {"base64": "A" * 300_000, "type": "image/png", "originalSize": 225_000}}
        call("pre_tool_use.py", {**base, "hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "C:/x/shot.png"}, "tool_use_id": "i1"})
        rc, out, ms = call("post_tool_use.py", {**base, "hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": "C:/x/shot.png"}, "tool_response": image_resp, "tool_use_id": "i1"})
        img = [e for e in led.iter(ev="tool.call") if e["data"].get("path") == "C:/x/shot.png"]
        checks.append(("image counted as a fixed estimate, not base64 bytes", bool(img) and img[-1]["data"]["out_bytes"] == 6400))
        checks.append(("image is not a FLOOD", "FLOOD" not in out))
        call("pre_tool_use.py", {**base, "hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "C:/x/huge.txt"}, "tool_use_id": "h1"})
        rc, out, ms = call("post_tool_use.py", {**base, "hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": "C:/x/huge.txt"}, "tool_response": {"type": "text", "file": {"filePath": "C:/x/huge.txt", "content": "z" * 30_000, "numLines": 900}}, "tool_use_id": "h1"})
        checks.append(("a real 30k-char text Read is still a FLOOD", "FLOOD" in out))

        # Stop: last_assistant_message takes priority over the transcript
        rc, out, ms = call("stop.py", {**base, "hook_event_name": "Stop", "transcript_path": str(tr), "last_assistant_message": "Done, everything is finished."}); lat["stop"] = ms
        checks.append(("stop non-strict silent", rc == 0 and out == ""))
        claims = list(led.iter(ev="claim"))
        checks.append(("stop wrote claim with 0 evidence", len(claims) == 1 and claims[0]["data"]["evidence_n"] == 0))
        checks.append(("stop wrote usage", any(e["data"].get("input") == 100 for e in led.iter(ev="usage"))))
        checks.append(("stop usage carries the request id", any(e["data"].get("req") == "r1" for e in led.iter(ev="usage"))))
        rc, out, ms = call("stop.py", {**base, "hook_event_name": "Stop", "transcript_path": str(tr), "last_assistant_message": "Done, everything is finished."}, env={"KEEL_STRICT": "1"})
        try:
            j = json.loads(out)
        except Exception:  # noqa: BLE001
            j = {}
        checks.append(("stop strict blocks unverified claim", j.get("decision") == "block" and j.get("hookSpecificOutput", {}).get("decision") == "block"))
        checks.append(("stop usage not duplicated", sum(1 for e in led.iter(ev="usage")) == 1))
        rc, out, ms = call("stop.py", {**base, "hook_event_name": "Stop", "transcript_path": str(tr), "last_assistant_message": "Done, everything is finished."}, env={"KEEL_STRICT": "1"})
        checks.append(("stop strict blocks at most once per turn", out == ""))
        rc, out, ms = call("stop.py", {**base, "hook_event_name": "Stop", "transcript_path": str(tr), "last_assistant_message": "Not done yet."}, env={"KEEL_STRICT": "1"})
        checks.append(("negated claim not blocked", out == ""))

        # subagent: transcript in <session>/subagents/agent-x.jsonl
        sub_dir = Path(td) / sid / "subagents"
        sub_dir.mkdir(parents=True)
        sub_tr = sub_dir / "agent-abc.jsonl"
        sub_lines = [
            {"type": "user", "timestamp": "2026-09-02T10:02:00Z", "isSidechain": True, "message": {"role": "user", "content": "task"}},
            {"type": "assistant", "timestamp": "2026-09-02T10:02:03Z", "isSidechain": True, "requestId": "s1",
             "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 1000},
                         "content": [{"type": "text", "text": "ok"}]}},
        ]
        sub_tr.write_text("\n".join(json.dumps(l) for l in sub_lines) + "\n", encoding="utf-8")
        rc, out, ms = call("subagent_stop.py", {**base, "hook_event_name": "SubagentStop", "agent_id": "abc", "agent_transcript_path": str(sub_tr)}); lat["subagent_stop"] = ms
        subs = [e for e in led.iter(ev="usage") if str(e["agent"]).startswith("sub")]
        checks.append(("subagent usage recorded", len(subs) == 1 and subs[0]["data"]["cache_read"] == 1000))
        call("subagent_stop.py", {**base, "hook_event_name": "SubagentStop", "agent_id": "abc", "agent_transcript_path": str(sub_tr)})
        checks.append(("subagent not double counted", sum(1 for e in led.iter(ev="usage") if str(e["agent"]).startswith("sub")) == 1))

        # R8: a subagent tool call recorded live (PostToolUse fires inside subagents) and again
        # in the subagent transcript is written once, also after the transcript grows
        sub2 = {**base, "agent_id": "def"}
        call("post_tool_use.py", {**sub2, "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {"stdout": "a"}, "tool_use_id": "st1"})
        sub_tr2 = sub_dir / "agent-def.jsonl"
        rows = [
            {"type": "user", "timestamp": "2026-09-02T10:03:00Z", "isSidechain": True, "message": {"role": "user", "content": "task"}},
            {"type": "assistant", "timestamp": "2026-09-02T10:03:01Z", "isSidechain": True, "requestId": "s2",
             "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 5, "output_tokens": 1},
                         "content": [{"type": "tool_use", "id": "st1", "name": "Bash", "input": {"command": "ls"}}]}},
            {"type": "user", "timestamp": "2026-09-02T10:03:02Z", "isSidechain": True, "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "st1", "content": "a"}]}},
        ]
        sub_tr2.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        call("subagent_stop.py", {**sub2, "hook_event_name": "SubagentStop", "agent_transcript_path": str(sub_tr2)})
        rows.append({"type": "assistant", "timestamp": "2026-09-02T10:03:03Z", "isSidechain": True, "requestId": "s3",
                     "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 5, "output_tokens": 1}, "content": [{"type": "text", "text": "ok"}]}})
        sub_tr2.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        call("subagent_stop.py", {**sub2, "hook_event_name": "SubagentStop", "agent_transcript_path": str(sub_tr2)})
        st1 = [e for e in led.iter(ev="tool.call") if e["data"].get("tool_use_id") == "st1"]
        checks.append(("R8 subagent tool call written once (live + transcript + regrown transcript)", len(st1) == 1))
        checks.append(("R8 regrown transcript usage written once per request", sum(1 for e in led.iter(ev="usage") if e["data"].get("req") in ("s2", "s3")) == 2))

        # R12: a read by one agent does not cover an edit by another agent
        call("post_tool_use.py", {**base, "agent_id": "A", "hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": "C:/x/shared.py"}, "tool_response": {"type": "text", "file": {"filePath": "C:/x/shared.py", "content": "x" * 300}}, "tool_use_id": "ra"})
        rc, out, _ = call("post_tool_use.py", {**base, "agent_id": "B", "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": "C:/x/shared.py", "old_string": "a", "new_string": "b"}, "tool_response": {"ok": True}, "tool_use_id": "eb"})
        checks.append(("R12 edit by agent B after a read by agent A is a blind edit", "BLIND_EDIT" in out))

        # R11: one message with six Agent calls gives at most one FANOUT incident
        before = sum(1 for e in led.iter(ev="incident") if e["data"]["cls"] == "FANOUT")
        for i in range(6):
            call("post_tool_use.py", {**base, "hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_input": {"prompt": f"p{i}"}, "tool_response": "ok", "tool_use_id": f"ag{i}"})
        after = sum(1 for e in led.iter(ev="incident") if e["data"]["cls"] == "FANOUT")
        checks.append(("R11 six Agent calls in one message: at most one FANOUT", after - before <= 1))
        checks.append(("R14 MEMORY_LOST at session start is an incident", any(e["data"]["cls"] == "MEMORY_LOST" for e in led.iter(ev="incident"))))

        rc, out, ms = call("session_end.py", {**base, "hook_event_name": "SessionEnd", "reason": "exit"}); lat["session_end"] = ms
        stops = [e for e in led.iter(ev="session.stop") if e["agent"] == "head"]
        checks.append(("session_end wrote stop", len(stops) == 1 and stops[0]["data"]["duration_s"] is not None))
        checks.append(("subagent session.stop marked sub", any(str(e["agent"]).startswith("sub") for e in led.iter(ev="session.stop"))))

        costs = list(led.iter(ev="layer.cost"))
        checks.append(("layer.cost recorded", len(costs) >= 8))
        checks.append(("ledger chain intact", led.audit()["ok"]))
        r = subprocess.run([sys.executable, str(HOOKS / "post_tool_use.py")], input="{not json", capture_output=True, text=True, encoding="utf-8")
        checks.append(("fail-open on bad stdin", r.returncode == 0))

        print("ADAPTER LATENCY (ms, cold python start on every call):")
        for k, v in lat.items():
            print(f"  {k:20s} {v:7.0f}")
        med = sorted(float(c["data"]["ms"]) for c in costs)
        if med:
            print(f"  adapter internal time (median of layer.cost): {med[len(med)//2]:.0f} ms")
        else:
            print("  layer.cost not written: ledger not found at the expected path")

    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        if not ok:
            print("FAIL", n)
    print(f"HOOKS-E2E: {'PASS' if not bad else 'FAIL'} {len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
