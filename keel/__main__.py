# -*- coding: utf-8 -*-
"""KEEL command line: python -m keel <command>.

    backfill   build a ledger from Claude Code transcript history
    vitals     vital signs of a ledger (json)
    panel      compact scoreboard (what the agent sees at session start)
    report     full markdown report for one ledger or a directory of ledgers
    audit      hash-chain integrity of a ledger (with an optional external checkpoint)
    export     copy a ledger for sharing; text-bearing fields are redacted by default
    doctor     interpreter start cost, ledger integrity, hook registration
    open       open a task contract
    verify     run the done-predicates of an open task (--ok for manual ones)
    close      close a task (verified is accepted only after verify)
    verdict    record a verdict; the CLI always writes src=cli, which does not count in V1
    incident   record an incident by hand (a class from the registry)
    classes    show the registry of incident classes
    selftest   red-team self test of both ends
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from . import __version__, contract, incidents, scoreboard, vitals
from .backfill import backfill_all
from .ledger import Ledger


def _since(days: float | None) -> datetime | None:
    return (datetime.now(timezone.utc) - timedelta(days=days)) if days else None


def _load_result(ledger_path: Path, days: float | None):
    led = Ledger(ledger_path)
    since = _since(days)
    ev = led.all(since=since)
    # a close inside the window may belong to an open (and verifies) before it
    contract_ev = led.all(ev=set(contract.CONTRACT_EVENTS)) if since else None
    return vitals.compute(ev, contract_events=contract_ev)


def _project_of(ledger_path: Path) -> str:
    for e in Ledger(ledger_path).iter():
        return str(e.get("project") or ledger_path.stem)
    return ledger_path.stem


def cmd_backfill(a: argparse.Namespace) -> int:
    res = backfill_all(Path(a.projects_dir).expanduser(), Path(a.out), only=a.only, days=a.days)
    print(json.dumps({"projects": len(res), "sessions": sum(r["sessions"] for r in res),
                      "events": sum(r["events"] for r in res)}, ensure_ascii=False))
    return 0


def cmd_vitals(a: argparse.Namespace) -> int:
    r = _load_result(Path(a.ledger), a.days)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0


def cmd_panel(a: argparse.Namespace) -> int:
    r = _load_result(Path(a.ledger), a.days)
    label = f"(window {a.days:g} d)" if a.days else "(whole ledger)"
    print(scoreboard.render_panel(r, title=f"KEEL SCOREBOARD: {_project_of(Path(a.ledger))}", window_label=label,
                                  show_balance=bool(a.balance)))
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    p = Path(a.ledger)
    label = f"last {a.days:g} days" if a.days else "whole ledger"
    parts = []
    if p.is_dir():
        for lp in sorted(p.glob("*.jsonl")):
            r = _load_result(lp, a.days)
            if r["events"] == 0:
                continue
            parts.append(scoreboard.render_report(r, title=f"Vital signs: {_project_of(lp)} ({lp.stem})", window_label=label))
    else:
        parts.append(scoreboard.render_report(_load_result(p, a.days), title=f"Vital signs: {_project_of(p)} ({p.stem})", window_label=label))
    text = "\n\n".join(parts)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"written {a.out}")
    else:
        print(text)
    return 0


def cmd_audit(a: argparse.Namespace) -> int:
    r = Ledger(a.ledger).audit(expect_count=a.expect_count, expect_last_hash=a.expect_last_hash)
    print(f"AUDIT: {'OK' if r['ok'] else 'FAIL'} events {r['events']}, breaks {r['breaks']}")
    for d in r["details"]:
        print(f"  line {d['line']}: {d['why']}")
    if r.get("last_hash"):
        print(f"checkpoint (store it where the agent cannot write; the chain alone does not detect a cut tail): "
              f"--expect-count {r['events']} --expect-last-hash {r['last_hash']}")
    return 0 if r["ok"] else 1


def cmd_export(a: argparse.Namespace) -> int:
    from . import schema
    from .ledger import redact_event
    if Path(a.out).resolve() == Path(a.ledger).resolve():
        print("REFUSED: --out must be another file")
        return 2
    n = 0
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        for e in Ledger(a.ledger).iter():
            fh.write(schema.dumps(e if a.no_redact else redact_event(e)) + "\n")
            n += 1
    if a.no_redact:
        print(f"exported {n} events to {a.out} WITHOUT redaction: goal text, predicates, command output tails, "
              f"verdict reasons, incident details and paths are included")
    else:
        print(f"exported {n} events to {a.out}; redacted: goal, predicates, verify evidence, verdict reasons and artifacts, "
              f"incident details, paths, cwd. The copy keeps the original hashes and does not pass audit.")
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    rc = 0
    xs = []
    for _ in range(3):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-S", "-E", "-c", "pass"], capture_output=True)
        xs.append((time.perf_counter() - t0) * 1000)
    ys = []
    for _ in range(3):
        t0 = time.perf_counter()
        subprocess.run([sys.executable, "-c", "pass"], capture_output=True)
        ys.append((time.perf_counter() - t0) * 1000)
    print(f"interpreter start: python {statistics.median(ys):.0f} ms, python -S -E {statistics.median(xs):.0f} ms "
          f"(not part of V13: it is a tax on top of the adapter's internal time)")
    lp = Path(a.ledger) if a.ledger else Path(".keel/ledger.jsonl")
    if lp.exists():
        r = Ledger(lp).audit()
        print(f"ledger {lp}: AUDIT {'OK' if r['ok'] else 'FAIL'} events {r['events']}, breaks {r['breaks']}")
        rc |= 0 if r["ok"] else 1
    else:
        print(f"ledger {lp}: no file (the layer has not run live)")
    # registration: the global file covers all projects, the project file only its own; pre_tool_use is optional
    required = ["session_start.py", "user_prompt_submit.py", "post_tool_use.py", "post_tool_use_failure.py",
                "subagent_stop.py", "stop.py", "session_end.py"]
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "~").expanduser()
    candidates = [("global", home / ".claude" / "settings.json"), ("project", Path(a.project or ".") / ".claude" / "settings.json")]
    covered: set[str] = set()
    for label, settings in candidates:
        if not settings.exists():
            print(f"registration {label}: {settings} is missing")
            continue
        txt = settings.read_text(encoding="utf-8", errors="replace")
        have = [n for n in required if n in txt]
        covered.update(have)
        extra = " + pre_tool_use.py" if "pre_tool_use.py" in txt else ""
        print(f"registration {label} ({settings}): {len(have)}/{len(required)}{extra}")
    missing = [n for n in required if n not in covered]
    print("adapters covered: " + (f"all {len(required)}" if not missing
                                  else f"{len(required) - len(missing)}/{len(required)}, missing: {', '.join(missing)}"))
    rc |= 1 if missing else 0
    return rc


def _ctx(a: argparse.Namespace) -> dict[str, str]:
    return {"session": a.session or os.environ.get("KEEL_SESSION", "cli"), "project": a.project or Path.cwd().name}


def cmd_open(a: argparse.Namespace) -> int:
    led = Ledger(a.ledger)
    budget = {}
    if a.budget_tokens:
        budget["tokens"] = a.budget_tokens
    if a.budget_minutes:
        budget["minutes"] = a.budget_minutes
    try:
        e = contract.open_task(led, task=a.task, goal=a.goal, dod=a.dod, budget=budget, forecast_p=a.p, zone=a.zone or [],
                               root=a.root, **_ctx(a))
    except contract.ContractError as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(json.dumps(e, ensure_ascii=False))
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    led = Ledger(a.ledger)
    manual = {}
    if a.ok:
        ev = a.evidence or []
        if len(ev) != len(a.ok):
            print("REFUSED: every --ok needs its own --evidence")
            return 2
        manual = dict(zip(a.ok, ev))
    try:
        out = contract.verify(led, task=a.task, cwd=(Path(a.cwd) if a.cwd else None), manual_ok=manual, **_ctx(a))
    except contract.ContractError as exc:
        print(f"REFUSED: {exc}")
        return 2
    ok_all = all(e["data"]["ok"] for e in out)
    for e in out:
        print(("OK   " if e["data"]["ok"] else "FAIL ") + e["data"]["predicate"] + "  <- " + e["data"]["evidence"])
    print("VERIFY: " + ("PASS" if ok_all else "FAIL") + f" {sum(1 for e in out if e['data']['ok'])}/{len(out)}")
    return 0 if ok_all else 1


def cmd_close(a: argparse.Namespace) -> int:
    led = Ledger(a.ledger)
    try:
        e = contract.close_task(led, task=a.task, outcome=a.outcome, evidence=a.evidence or [], **_ctx(a))
    except contract.ContractError as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(json.dumps(e, ensure_ascii=False))
    return 0


def cmd_verdict(a: argparse.Namespace) -> int:
    led = Ledger(a.ledger)
    try:
        e = contract.record_verdict(led, artifact=a.artifact, verdict=a.verdict, reasons=a.reason or [], src="cli",
                                    claimed_by=a.by, **_ctx(a))
    except contract.ContractError as exc:
        print(f"REFUSED: {exc}")
        return 2
    print("NOTE: recorded with src=cli. The CLI cannot record an owner-channel verdict, so this does not count in V1; "
          "an owner-side integration writes those through keel.contract.record_verdict.")
    print(json.dumps(e, ensure_ascii=False))
    return 0


def cmd_incident(a: argparse.Namespace) -> int:
    from . import schema
    if a.cls not in incidents.CLASSES:
        print(f"REFUSED: class {a.cls!r} is not in the registry; python -m keel classes")
        return 2
    led = Ledger(a.ledger)
    hist = {e["data"]["cls"] for e in led.iter(ev="incident")}
    c = incidents.CLASSES[a.cls]
    e = schema.make_event("incident", src="cli", **_ctx(a),
                          data={"cls": a.cls, "severity": c["severity"], "repeat": a.cls in hist, "detail": {"text": a.detail}})
    led.append(e)
    print(json.dumps(e, ensure_ascii=False))
    return 0


def cmd_classes(a: argparse.Namespace) -> int:
    for k, c in incidents.CLASSES.items():
        print(f"{k:24s} {c['severity']:6s} {c['detect']:18s} {c['name']}")
    return 0


def cmd_selftest(a: argparse.Namespace) -> int:
    from .selftest import run
    return run()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="keel", description=f"KEEL {__version__}: a base layer for agents")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("backfill"); s.add_argument("--projects-dir", default="~/.claude/projects")
    s.add_argument("--out", default="data/ledger"); s.add_argument("--only", nargs="*"); s.add_argument("--days", type=float)
    s.set_defaults(fn=cmd_backfill)

    for name, fn in (("vitals", cmd_vitals), ("panel", cmd_panel)):
        s = sub.add_parser(name); s.add_argument("--ledger", required=True); s.add_argument("--days", type=float)
        s.add_argument("--balance", action="store_true", help="print the balance (points as a sum of events)"); s.set_defaults(fn=fn)

    s = sub.add_parser("report"); s.add_argument("--ledger", required=True, help="a ledger file or a directory of ledgers")
    s.add_argument("--days", type=float); s.add_argument("--out"); s.set_defaults(fn=cmd_report)

    s = sub.add_parser("audit"); s.add_argument("--ledger", required=True)
    s.add_argument("--expect-count", type=int, help="events in an earlier audit (external checkpoint)")
    s.add_argument("--expect-last-hash", help="last_hash of that earlier audit (full, or at least 16 chars)")
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("export"); s.add_argument("--ledger", required=True); s.add_argument("--out", required=True)
    s.add_argument("--no-redact", action="store_true", help="copy as is, including goal text, command output tails and paths")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("doctor"); s.add_argument("--ledger"); s.add_argument("--project"); s.set_defaults(fn=cmd_doctor)

    def ctx(p: argparse.ArgumentParser) -> None:
        p.add_argument("--ledger", required=True); p.add_argument("--session"); p.add_argument("--project")

    s = sub.add_parser("open"); ctx(s); s.add_argument("--task", required=True); s.add_argument("--goal", required=True)
    s.add_argument("--dod", nargs="+", required=True); s.add_argument("--budget-tokens", type=int); s.add_argument("--budget-minutes", type=int)
    s.add_argument("--p", type=float); s.add_argument("--zone", nargs="*")
    s.add_argument("--root", help="project root the predicates run in (default: the current directory); pinned at open")
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("verify"); ctx(s); s.add_argument("--task", required=True)
    s.add_argument("--cwd", help="optional; must equal the root pinned at open")
    s.add_argument("--ok", nargs="*", help="manual predicates manual:... that are closed by a word")
    s.add_argument("--evidence", nargs="*", help="evidence for each --ok, in the same order"); s.set_defaults(fn=cmd_verify)

    s = sub.add_parser("close"); ctx(s); s.add_argument("--task", required=True)
    s.add_argument("--outcome", required=True, choices=["verified", "claimed", "blocked", "abandoned"])
    s.add_argument("--evidence", nargs="*"); s.set_defaults(fn=cmd_close)

    s = sub.add_parser("verdict"); ctx(s); s.add_argument("--artifact", required=True)
    s.add_argument("--verdict", required=True, choices=["accept", "reject"]); s.add_argument("--reason", nargs="*")
    s.add_argument("--by", help="who gave the verdict, recorded as a claim (data.claimed_by); the source stays cli")
    s.set_defaults(fn=cmd_verdict)

    s = sub.add_parser("incident"); ctx(s); s.add_argument("--cls", required=True); s.add_argument("--detail", default=""); s.set_defaults(fn=cmd_incident)

    s = sub.add_parser("classes"); s.set_defaults(fn=cmd_classes)
    s = sub.add_parser("selftest"); s.set_defaults(fn=cmd_selftest)

    a = ap.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
