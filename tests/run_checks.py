# -*- coding: utf-8 -*-
"""Run every check and print the tail of each one's output. Exit code = the worst exit code.

Run: python tests/run_checks.py
Every probe writes its ledgers into a temporary KEEL_HOME, never into your real ~/.keel.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

COMMANDS = [
    ("python -m keel selftest", [sys.executable, "-m", "keel", "selftest"]),
    ("python -m pytest tests -q", [sys.executable, "-m", "pytest", "tests", "-q"]),
    ("python tests/hooks_e2e.py", [sys.executable, "tests/hooks_e2e.py"]),
    ("python tests/adversarial/a2_contract_bypass.py", [sys.executable, "tests/adversarial/a2_contract_bypass.py"]),
    ("python tests/adversarial/a8_state_race.py", [sys.executable, "tests/adversarial/a8_state_race.py"]),
    ("python tests/adversarial/a9_ledger_concurrency.py", [sys.executable, "tests/adversarial/a9_ledger_concurrency.py"]),
]


def main() -> int:
    os.environ["KEEL_HOME"] = tempfile.mkdtemp(prefix="keel_checks_")
    worst = 0
    for title, cmd in COMMANDS:
        try:
            r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
            text = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")
            rc = r.returncode
        except subprocess.TimeoutExpired:
            text, rc = "TIMEOUT after 900 s", 124
        tail = text.strip().splitlines()[-15:]
        print(f"\n## {title}  (exit code {rc})")
        print("\n".join(tail))
        worst = max(worst, rc)
    print(f"\nrun_checks: {'PASS' if worst == 0 else 'FAIL'} (worst exit code {worst})")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
