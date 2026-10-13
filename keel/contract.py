# -*- coding: utf-8 -*-
"""Intent contract: a task is opened with done-predicates and closed with evidence.

The point: "10 out of 10" cannot be checked, "file X exists and is longer than 1 MB, command Y
returned 0, the owner said accept" can. The agent writes the contract BEFORE the work, and that
is the only way the layer learns what the owner wanted and the agent learns where it is going.

Protection against inflation:
    * predicates are immutable: `dod_hash` is computed at open; a second `task.open` with the
      same id while the task is open is refused, and `find_open` takes the FIRST open after the
      last close, so a "reopen" appended to the ledger does not swap the predicates;
    * `verify` carries the `dod_hash` of the open contract; `close --outcome verified` is
      accepted only if EVERY predicate has a verify ok=true with the same hash after the open;
      `task.close` itself carries dod and dod_hash, and `keel.vitals` re-checks verified against
      the ledger;
    * the ledger is hash-chained (`keel.ledger.audit`), forged lines are visible;
    * an accept verdict written from the CLI is marked with source `cli` and does NOT count in
      V1: the trusted sources are only `tg`, `owner`, `owner-file`.

Predicates (a string of the form `type:arguments`):
    file_exists:<path>
    file_min_bytes:<path>:<n>
    file_newer_than:<path>:<ISO time>
    regex_in_file:<path>:<regex>
    cmd_exit0:<shell command>              (runs in the contract's cwd, 300 s timeout)
    owner_accept:<artifact>                (looks for a trusted owner.verdict accept for the artifact)
    manual:<text>                          (closed only by `verify --ok <predicate> --evidence <evidence>`)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from . import schema
from .ledger import Ledger

TRUSTED_VERDICT_SRC = ("tg", "owner", "owner-file")


class ContractError(RuntimeError):
    pass


def dod_hash(dod: list[str]) -> str:
    return hashlib.sha256(json.dumps(list(dod), ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def find_open(ledger: Ledger, task: str) -> dict[str, Any] | None:
    """The first task.open after the last task.close for the task; later opens are ignored."""
    opened = None
    for e in ledger.iter(where=lambda x: x.get("task") == task):
        if e["ev"] == "task.open" and opened is None:
            opened = e
        elif e["ev"] == "task.close":
            opened = None
    return opened


def open_task(
    ledger: Ledger, *, session: str, project: str, task: str, goal: str, dod: list[str],
    budget: dict[str, Any] | None = None, forecast_p: float | None = None, zone: list[str] | None = None,
    src: str = "cli", agent: str = "head",
) -> dict[str, Any]:
    if not dod:
        raise ContractError("a contract without done-predicates is not opened: say how you will prove it")
    for p in dod:
        if ":" not in p or p.split(":", 1)[0] not in PREDICATES:
            raise ContractError(f"unknown predicate {p!r}; allowed: {', '.join(PREDICATES)}")
    if forecast_p is not None and not (0.0 <= forecast_p <= 1.0):
        raise ContractError("forecast_p must be in [0,1]")
    if find_open(ledger, task) is not None:
        raise ContractError(f"task {task!r} is already open: predicates do not change after opening; close it and open a new one with another id")
    e = schema.make_event(
        "task.open", session=session, project=project, agent=agent, src=src, task=task,
        data={"goal": goal, "dod": list(dod), "dod_hash": dod_hash(dod), "budget": budget or {},
              "forecast_p": forecast_p, "zone": zone or []},
    )
    ledger.append(e)
    return e


# ---- predicates ----------------------------------------------------------------

def _p_file_exists(arg: str, cwd: Path) -> tuple[bool, str]:
    p = (cwd / arg) if not os.path.isabs(arg) else Path(arg)
    return p.exists(), f"{p} exists={p.exists()}"


def _p_file_min_bytes(arg: str, cwd: Path) -> tuple[bool, str]:
    path, n = arg.rsplit(":", 1)
    p = (cwd / path) if not os.path.isabs(path) else Path(path)
    if not p.exists():
        return False, f"{p} is missing"
    size = p.stat().st_size
    return size >= int(n), f"{p} bytes={size} (need >= {n})"


def _p_file_newer_than(arg: str, cwd: Path) -> tuple[bool, str]:
    path, iso = arg.split(":", 1)
    p = (cwd / path) if not os.path.isabs(path) else Path(path)
    if not p.exists():
        return False, f"{p} is missing"
    mtime = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
    ref = schema.parse_ts(iso)
    return mtime >= ref, f"{p} mtime={mtime.isoformat(timespec='seconds')} (need >= {iso})"


def _p_regex_in_file(arg: str, cwd: Path) -> tuple[bool, str]:
    path, rx = arg.split(":", 1)
    p = (cwd / path) if not os.path.isabs(path) else Path(path)
    if not p.exists():
        return False, f"{p} is missing"
    text = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(rx, text, re.MULTILINE)
    return bool(m), f"{p} regex={rx!r} match={bool(m)}" + (f" -> {m.group(0)[:80]!r}" if m else "")


def _p_cmd_exit0(arg: str, cwd: Path) -> tuple[bool, str]:
    try:
        r = subprocess.run(arg, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"cmd {arg!r} timed out after 300 s"
    tail = (r.stdout or "")[-300:].strip().replace("\n", " | ")
    return r.returncode == 0, f"cmd {arg!r} rc={r.returncode} out={tail!r}"


def _p_owner_accept(arg: str, cwd: Path, ledger: Ledger | None = None) -> tuple[bool, str]:
    if ledger is None:
        return False, "no ledger to look up the verdict in"
    last = None
    for e in ledger.iter(ev="owner.verdict", where=lambda x: x["data"].get("artifact") == arg and x.get("src") in TRUSTED_VERDICT_SRC):
        last = e
    if last is None:
        return False, f"no trusted owner verdict for {arg!r} (sources: {', '.join(TRUSTED_VERDICT_SRC)})"
    ok = last["data"].get("verdict") == "accept"
    return ok, f"owner.verdict {last['data'].get('verdict')} at {last['ts']} (src={last.get('src')})"


def _p_manual(arg: str, cwd: Path) -> tuple[bool, str]:
    return False, f"manual predicate {arg!r}: closed only by verify --ok with evidence"


PREDICATES = {
    "file_exists": _p_file_exists,
    "file_min_bytes": _p_file_min_bytes,
    "file_newer_than": _p_file_newer_than,
    "regex_in_file": _p_regex_in_file,
    "cmd_exit0": _p_cmd_exit0,
    "owner_accept": _p_owner_accept,
    "manual": _p_manual,
}


def verify(ledger: Ledger, *, task: str, cwd: Path, session: str, project: str, src: str = "cli",
           manual_ok: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Runs every predicate of the open contract, writes verify events, returns them.
    manual_ok: {predicate manual:... : evidence} closes manual predicates with an explicit word plus evidence."""
    opened = find_open(ledger, task)
    if opened is None:
        raise ContractError(f"task {task!r} is not open or is already closed")
    h = opened["data"].get("dod_hash") or dod_hash(opened["data"]["dod"])
    manual_ok = manual_ok or {}
    for pred in manual_ok:
        if not pred.startswith("manual:") or pred not in opened["data"]["dod"]:
            raise ContractError(f"--ok is allowed only for a manual predicate from the contract, not for {pred!r}")
        if not manual_ok[pred].strip():
            raise ContractError(f"manual predicate {pred!r} is not closed without evidence")
    out = []
    for pred in opened["data"]["dod"]:
        kind, arg = pred.split(":", 1)
        fn = PREDICATES[kind]
        try:
            if pred in manual_ok:
                ok, evidence = True, "manual close: " + manual_ok[pred].strip()
            elif kind == "owner_accept":
                ok, evidence = fn(arg, cwd, ledger)  # type: ignore[call-arg]
            else:
                ok, evidence = fn(arg, cwd)
        except Exception as exc:  # noqa: BLE001
            ok, evidence = False, f"check error: {exc}"
        e = schema.make_event("verify", session=session, project=project, src=src, task=task,
                              data={"predicate": pred, "ok": bool(ok), "evidence": evidence, "dod_hash": h})
        ledger.append(e)
        out.append(e)
    return out


def proven_predicates(ledger: Ledger, *, task: str, opened: dict[str, Any]) -> dict[str, bool]:
    """The last verify verdict for each predicate of the open contract, only with its hash
    and only after the open."""
    h = opened["data"].get("dod_hash") or dod_hash(opened["data"]["dod"])
    t_open = schema.parse_ts(opened["ts"])
    last: dict[str, bool] = {}
    for e in ledger.iter(ev="verify", where=lambda x: x.get("task") == task):
        if e["data"].get("dod_hash") != h:
            continue
        if schema.parse_ts(e["ts"]) < t_open:
            continue
        last[e["data"]["predicate"]] = bool(e["data"].get("ok"))
    return last


def close_task(ledger: Ledger, *, task: str, outcome: str, session: str, project: str,
               evidence: list[str] | None = None, src: str = "cli") -> dict[str, Any]:
    opened = find_open(ledger, task)
    if opened is None:
        raise ContractError(f"task {task!r} is not open or is already closed")
    if outcome not in ("verified", "claimed", "blocked", "abandoned"):
        raise ContractError("outcome: verified | claimed | blocked | abandoned")
    h = opened["data"].get("dod_hash") or dod_hash(opened["data"]["dod"])
    if outcome == "verified":
        last = proven_predicates(ledger, task=task, opened=opened)
        missing = [p for p in opened["data"]["dod"] if not last.get(p)]
        if missing:
            raise ContractError("outcome verified is not accepted: predicates not proven: " + "; ".join(missing)
                                + ". Close as claimed or run verify.")
    e = schema.make_event("task.close", session=session, project=project, src=src, task=task,
                          data={"outcome": outcome, "evidence": evidence or [], "forecast_p": opened["data"].get("forecast_p"),
                                "goal": opened["data"].get("goal"), "dod": list(opened["data"]["dod"]), "dod_hash": h})
    ledger.append(e)
    return e


def record_verdict(ledger: Ledger, *, session: str, project: str, artifact: str, verdict: str,
                   reasons: list[str], src: str = "cli") -> dict[str, Any]:
    if verdict not in ("accept", "reject"):
        raise ContractError("verdict: accept | reject")
    if verdict == "reject" and not reasons:
        raise ContractError("a reject without reasons is not recorded: reasons verbatim, in the owner's own words")
    e = schema.make_event("owner.verdict", session=session, project=project, src=src,
                          data={"artifact": artifact, "verdict": verdict, "reasons": reasons,
                                "verbatim": src in TRUSTED_VERDICT_SRC, "trusted": src in TRUSTED_VERDICT_SRC})
    ledger.append(e)
    return e
