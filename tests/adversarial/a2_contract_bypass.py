# -*- coding: utf-8 -*-
"""A2. Attempts to reach "verified" / "accept" without doing the work. Every attempt must FAIL.

Attempts:
    1. close --outcome verified without any verify                          (must be refused)
    2. a forged task.close verified appended straight into the JSONL        (V2 must not count it)
    3. forged verify events (no contract hash), then a regular close        (must be refused)
    4. a second task.open with the same id and an easy predicate            (must be refused / ignored)
    5. `verdict --verdict accept` run by the agent (src=cli)                (V1 must stay NO DATA)
    6. forecast_p = 0.5 always                                              (V14 must say NO DATA, not OK)
    7. a manual predicate can be closed only with evidence                  (must be refused without it)
    8. an edited ledger line                                                (audit must fail)
    9. `verdict --src owner` from the CLI                                   (must be refused, nothing counts in V1)
   10. reopen the same task id and close it with the previous run's verify  (must be refused)
   11. a verify appended AFTER a forged close                               (V2 must not count it)
   12. verify the pinned contract from another directory                    (must be refused)

Run: python tests/adversarial/a2_contract_bypass.py   (exit code 0 = every defence held)
Works in a temporary directory only; it never touches a real ledger.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from keel import schema, vitals  # noqa: E402
from keel.ledger import Ledger  # noqa: E402

RES: list[tuple[str, bool]] = []  # (attempt, defence held)


def keel(*args: str, cwd: Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "keel", *args], cwd=str(cwd), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    return r.returncode, (r.stdout + r.stderr).strip()


def held(name: str, ok: bool, detail: str = "") -> None:
    RES.append((name, ok))
    print(f"[{'held  ' if ok else 'BROKEN'}] {name} {detail}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        common = ["--session", "s", "--project", "p"]

        # 1. close verified without verify
        p1 = wd / "l1.jsonl"
        keel("open", "--ledger", str(p1), *common, "--task", "T1", "--goal", "deliverable",
             "--dod", "file_min_bytes:out.mp4:1000000", "--p", "0.9", cwd=wd)
        rc, out = keel("close", "--ledger", str(p1), *common, "--task", "T1", "--outcome", "verified", cwd=wd)
        held("1. close verified without verify", rc != 0, f"(rc={rc})")

        # 2. a forged task.close verified appended straight into the ledger
        led2 = Ledger(wd / "l2.jsonl")
        led2.append(schema.make_event("task.close", session="s", project="p", src="cli", task="T1",
                                      data={"outcome": "verified", "evidence": ["trust me"], "forecast_p": 0.9, "goal": "x"}))
        v2 = vitals.compute(led2.all())["vitals"]["V2_verified_done"]
        held("2. forged task.close verified in the ledger", v2["value"] == 0.0 and v2["extra"]["unproven_verified"] == 1, f"(V2={v2['value']})")

        # 3. forged verify without the contract hash, then a regular close
        p3 = wd / "l3.jsonl"
        led3 = Ledger(p3)
        keel("open", "--ledger", str(p3), *common, "--task", "T2", "--goal", "deliverable",
             "--dod", "file_min_bytes:out.mp4:1000000", "--p", "0.9", cwd=wd)
        led3.append(schema.make_event("verify", session="s", project="p", src="cli", task="T2",
                                      data={"predicate": "file_min_bytes:out.mp4:1000000", "ok": True, "evidence": "trust me"}))
        rc, out = keel("close", "--ledger", str(p3), *common, "--task", "T2", "--outcome", "verified", cwd=wd)
        held("3. forged verify without the contract hash", rc != 0, f"(rc={rc})")

        # 4. a second task.open with an easy predicate
        p4 = wd / "l4.jsonl"
        keel("open", "--ledger", str(p4), *common, "--task", "T3", "--goal", "ten minute tape",
             "--dod", "file_min_bytes:out/tape.mp4:100000000", "--p", "0.9", cwd=wd)
        rc2, _ = keel("open", "--ledger", str(p4), *common, "--task", "T3", "--goal", "ten minute tape",
                      "--dod", "cmd_exit0:echo ok", "--p", "0.9", cwd=wd)
        keel("verify", "--ledger", str(p4), *common, "--task", "T3", "--cwd", str(wd), cwd=wd)
        rc4, _ = keel("close", "--ledger", str(p4), *common, "--task", "T3", "--outcome", "verified", cwd=wd)
        held("4. swapping predicates with a second task.open", rc2 != 0 and rc4 != 0, f"(reopen rc={rc2}, close rc={rc4})")

        # 5. verdict accept written by the agent
        p5 = wd / "l5.jsonl"
        keel("verdict", "--ledger", str(p5), *common, "--artifact", "out/tape.mp4", "--verdict", "accept", cwd=wd)
        v1 = vitals.compute(Ledger(p5).all())["vitals"]["V1_accepted"]
        held("5. owner.verdict accept from the CLI", v1["status"] == "NO DATA" and v1["extra"]["untrusted_accepts"] == 1, f"(V1={v1['status']})")

        # 6. forecast_p = 0.5 always
        led6 = Ledger(wd / "l6.jsonl")
        t0 = datetime(2026, 9, 2, tzinfo=timezone.utc)
        for i in range(8):
            led6.append(schema.make_event("task.close", session="s", project="p", src="cli", task=f"X{i}",
                                          ts=t0.isoformat(timespec="seconds"),
                                          data={"outcome": "verified" if i % 2 else "claimed", "forecast_p": 0.5}))
        v14 = vitals.compute(led6.all())["vitals"]["V14_calibration_brier"]
        held("6. forecast_p = 0.5 always", v14["status"] == "NO DATA", f"(V14={v14['status']}, extra={v14['extra']})")

        # 7. a manual predicate without evidence
        p7 = wd / "l7.jsonl"
        keel("open", "--ledger", str(p7), *common, "--task", "T4", "--goal", "manual", "--dod", "manual:owner listened", cwd=wd)
        rc, out = keel("verify", "--ledger", str(p7), *common, "--task", "T4", "--ok", "manual:owner listened", "--evidence", "  ", cwd=wd)
        held("7. manual predicate closed without evidence", rc != 0, f"(rc={rc})")

        # 8. the ledger itself: an edited line is caught by the hash chain
        p8 = wd / "l8.jsonl"
        led8 = Ledger(p8)
        for i in range(3):
            led8.append(schema.make_event("incident", session="s", project="p", src="cli",
                                          data={"cls": "STALL", "severity": "high", "repeat": False, "detail": {"minutes": 40 + i}}))
        lines = p8.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"repeat":false', '"repeat":true')
        p8.write_text("\n".join(lines) + "\n", encoding="utf-8")
        held("8. editing a ledger line", not led8.audit()["ok"], "(hash chain)")

        # 9. the agent claims owner origin from the CLI
        p9 = wd / "l9.jsonl"
        rc9, _ = keel("verdict", "--ledger", str(p9), *common, "--artifact", "out/tape.mp4", "--verdict", "accept", "--src", "owner", cwd=wd)
        v1b = vitals.compute(Ledger(p9).all())["vitals"]["V1_accepted"]
        held("9. verdict --src owner from the CLI", rc9 != 0 and v1b["status"] == "NO DATA", f"(rc={rc9}, V1={v1b['status']})")

        # 10. reopen the same task id and reuse the previous run's verify (same second, same predicates)
        p10 = wd / "l10.jsonl"
        (wd / "r10.txt").write_text("x" * 20, encoding="utf-8")
        keel("open", "--ledger", str(p10), *common, "--task", "T10", "--goal", "g", "--dod", "file_min_bytes:r10.txt:5", cwd=wd)
        keel("verify", "--ledger", str(p10), *common, "--task", "T10", cwd=wd)
        keel("close", "--ledger", str(p10), *common, "--task", "T10", "--outcome", "verified", cwd=wd)
        keel("open", "--ledger", str(p10), *common, "--task", "T10", "--goal", "g", "--dod", "file_min_bytes:r10.txt:5", cwd=wd)
        rc10, _ = keel("close", "--ledger", str(p10), *common, "--task", "T10", "--outcome", "verified", cwd=wd)
        held("10. reopened task closed with the previous run's verify", rc10 != 0, f"(rc={rc10})")

        # 11. open, forged close verified, then a verify appended after the close
        led11 = Ledger(wd / "l11.jsonl")
        o11 = led11.append(schema.make_event("task.open", session="s", project="p", src="cli", task="T11",
                                             data={"goal": "g", "dod": ["file_exists:x"], "dod_hash": "h11", "open_id": "o11"}))
        led11.append(schema.make_event("task.close", session="s", project="p", src="cli", task="T11",
                                       data={"outcome": "verified", "dod": ["file_exists:x"], "dod_hash": "h11", "open_id": "o11"}))
        led11.append(schema.make_event("verify", session="s", project="p", src="cli", task="T11",
                                       data={"predicate": "file_exists:x", "ok": True, "evidence": "late", "dod_hash": "h11", "open_id": "o11"}))
        v2b = vitals.compute(led11.all())["vitals"]["V2_verified_done"]
        held("11. verify appended after the close", o11 is not None and v2b["value"] == 0.0, f"(V2={v2b['value']})")

        # 12. verify a pinned contract from another directory where the predicate happens to hold
        other = wd / "other"
        other.mkdir()
        (other / "only_here.txt").write_text("x", encoding="utf-8")
        p12 = wd / "l12.jsonl"
        keel("open", "--ledger", str(p12), *common, "--task", "T12", "--goal", "g", "--dod", "file_exists:only_here.txt", cwd=wd)
        rc12v, _ = keel("verify", "--ledger", str(p12), *common, "--task", "T12", "--cwd", str(other), cwd=wd)
        rc12, _ = keel("close", "--ledger", str(p12), *common, "--task", "T12", "--outcome", "verified", cwd=wd)
        held("12. verify of a pinned contract from another directory", rc12v != 0 and rc12 != 0, f"(verify rc={rc12v}, close rc={rc12})")

    broken = [n for n, ok in RES if not ok]
    print(f"\nA2: {'PASS' if not broken else 'FAIL'} ({len(RES) - len(broken)}/{len(RES)} defences held)")
    return 0 if not broken else 1


if __name__ == "__main__":
    raise SystemExit(main())
