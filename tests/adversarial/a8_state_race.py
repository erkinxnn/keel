# -*- coding: utf-8 -*-
"""A8. Race for the session state under parallel tool calls.

Claude Code can issue several tool_use blocks in one message, and the harness runs
PreToolUse/PostToolUse in parallel. The layer's session state (`<keel dir>/state/<session>.json`)
is read, modified and rewritten as a whole; without a lock, parallel processes lose each other's
writes.

What would be lost: pending (call duration and path), read_paths (BLIND_EDIT), said (repeated
advice), seen_req (usage de-duplication in Stop), turn.

Run: python tests/adversarial/a8_state_race.py   (exit code 0 = no write lost)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "keel" / "hooks"
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from keel.ledger import Ledger  # noqa: E402

N = 8


def call(hook: str, payload: dict) -> int:
    r = subprocess.run([sys.executable, str(HOOKS / hook)], input=json.dumps(payload, ensure_ascii=False),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=dict(os.environ),
                       timeout=120)
    return r.returncode


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        # the ledger and state live OUTSIDE the project: ~/.keel/<encoded cwd>/ (or KEEL_HOME);
        # the path is taken from the layer itself
        os.environ["KEEL_HOME"] = str(Path(td) / "keelhome")
        sys.path.insert(0, str(HOOKS))
        from _common import keel_dir  # noqa: PLC0415
        home = Path(keel_dir(td))
        base = {"session_id": "race", "cwd": td}
        print(f"=== {N} parallel PreToolUse (like one model message with {N} calls) ===")
        with ThreadPoolExecutor(max_workers=N) as ex:
            list(ex.map(lambda i: call("pre_tool_use.py", {
                **base, "hook_event_name": "PreToolUse", "tool_name": "Read",
                "tool_input": {"file_path": f"C:/x/f{i}.py"}, "tool_use_id": f"t{i}"}), range(N)))
        st = json.loads((home / "state" / "race.json").read_text(encoding="utf-8"))
        print(f"  pending written: {len(st.get('pending', {}))} of {N} -> {sorted(st.get('pending', {}))}")
        lost_pre = N - len(st.get("pending", {}))
        print(f"  pending writes LOST: {lost_pre}")

        print(f"\n=== {N} parallel PostToolUse ===")
        with ThreadPoolExecutor(max_workers=N) as ex:
            list(ex.map(lambda i: call("post_tool_use.py", {
                **base, "hook_event_name": "PostToolUse", "tool_name": "Read",
                "tool_input": {"file_path": f"C:/x/f{i}.py"},
                "tool_response": {"content": "x" * 100}, "tool_use_id": f"t{i}"}), range(N)))
        led = Ledger(home / "ledger.jsonl")
        calls = led.all(ev="tool.call")
        print(f"  tool.call events in the ledger: {len(calls)} of {N} (the ledger is append-only, nothing should be lost here)")
        bad_path = [c for c in calls if c["data"].get("path") is None]
        print(f"  calls whose path was lost (pending not found): {len(bad_path)}")
        print(f"  bad_lines when reading the ledger: {led.bad_lines}")

        if lost_pre:
            print(f"\nFINDING: {lost_pre} of {N} state writes were lost on a single model message.")
            print("  Consequence: call duration (STALL) and path (BLIND_EDIT) of the lost calls fall back")
            print("  to an approximation, and the same mechanism breaks seen_req (usage de-duplication).")
        # both ends: state writes are not lost AND the ledger loses no events
        ok = (lost_pre == 0) and (len(calls) == N) and (led.bad_lines == 0) and not bad_path
        print(f"\nA8: {'PASS' if ok else 'FAIL'} (pending {N - lost_pre}/{N}, tool.call {len(calls)}/{N}, corrupt lines {led.bad_lines})")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
