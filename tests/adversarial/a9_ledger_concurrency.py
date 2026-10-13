# -*- coding: utf-8 -*-
"""A9. Does the ledger lose events under concurrent writes?

docs/ARCHITECTURE.md: the ledger is append-only JSONL with fsync, streaming reads with filters,
corrupt lines are counted and do not crash the read. keel/ledger.py: "the ledger is evidence".

Claude Code can issue several tool_use blocks in one model message and runs PostToolUse in
parallel: several processes append to the same file at once. This probe sets up exactly that
mode and counts how many events arrive.

Run: python tests/adversarial/a9_ledger_concurrency.py [writers] [events per writer]
Exit code 0 = nothing lost, no corrupt line, hash chain intact.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from keel.ledger import Ledger  # noqa: E402

WRITER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[3])
    from keel import schema
    from keel.ledger import Ledger
    led = Ledger(sys.argv[1])
    who = sys.argv[2]
    n = int(sys.argv[4])
    for i in range(n):
        led.append(schema.make_event("claim", session=who, project="p", data={"kind": "done", "i": i}))
    """
)


def trial(writers: int, per: int) -> tuple[int, int, int, int, bool]:
    with tempfile.TemporaryDirectory() as td:
        sc = Path(td) / "w.py"
        sc.write_text(WRITER, encoding="utf-8")
        lp = Path(td) / "l.jsonl"

        def run(i: int):
            return subprocess.run([sys.executable, str(sc), str(lp), f"w{i}", str(ROOT), str(per)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace")

        rs = list(ThreadPoolExecutor(max_workers=writers).map(run, range(writers)))
        rc_bad = sum(1 for r in rs if r.returncode != 0)
        led = Ledger(lp)
        got = led.all()
        raw = len([x for x in lp.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()])
        return len(got), led.bad_lines, raw, rc_bad, led.audit()["ok"]


def main() -> int:
    writers = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    per = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    print("Check: several processes append to one ledger (like parallel PostToolUse).")
    print(f"{'writers':>8} | {'expected':>9} | {'lines':>7} | {'parsed':>7} | {'corrupt':>8} | {'LOST':>5} | {'crashes':>8} | chain")
    print("-" * 82)
    worst = 0
    broken = 0
    for w in (1, 2, 4, writers):
        exp = w * per
        ok, bad, raw, rc_bad, chain_ok = trial(w, per)
        lost = exp - ok
        worst = max(worst, lost)
        broken += (not chain_ok) + bad + rc_bad
        print(f"{w:>8} | {exp:>9} | {raw:>7} | {ok:>7} | {bad:>8} | {lost:>5} | {rc_bad:>8} | {'ok' if chain_ok else 'BROKEN'}")
    print()
    if worst > 0 or broken:
        print("FINDING: Ledger.append is not atomic between processes (events lost, lines torn or chain broken).")
        return 1
    print("A9: PASS (no events lost, no corrupt lines, chain intact)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
