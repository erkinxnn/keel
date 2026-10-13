# -*- coding: utf-8 -*-
"""Intent contract: a task is opened with done-predicates and closed with evidence.

The point: "10 out of 10" cannot be checked, "file X exists and is longer than 1 MB, command Y
returned 0" can. The agent writes the contract BEFORE the work.

Protection against inflation, and where it stops:
    * every `task.open` gets a unique `open_id` and pins the absolute project root (`root`); the
      contract hash `dod_hash` covers the predicates AND the root, so the same predicate text in
      another checkout is another contract;
    * a second `task.open` with the same task id while the task is open is refused, and the replay
      takes the FIRST open after the last close, so a "reopen" appended to the ledger does not
      swap the predicates;
    * `verify` runs the predicates in the pinned root (a different `cwd` is refused) and every
      verify event carries the `open_id` and `dod_hash` of the contract it checked;
    * `close --outcome verified` is accepted only if, replaying the ledger IN ORDER, the latest
      verify of every predicate between this open and the close passed (`replay`); the check and
      the close write happen under the ledger's append lock, so a concurrent verify cannot slip in
      between them; a late close of an earlier open (another open_id) does not end the current one. `keel.vitals`
      uses the same replay for V2, so a verify written after the close, a failure after a pass,
      or a verify of an earlier run of the same task id does not count;
    * the ledger is hash-chained (`keel.ledger.audit`): an edited line inside the chain is visible;
    * the CLI always records verdicts with `src=cli`. The sources `tg`, `owner`, `owner-file` can be
      written only through the Python API (`record_verdict(src=...)`), meant for an owner-side
      integration such as a messaging bot. KEEL ships no such integration and the ledger is not
      authenticated: any process that can write the ledger file can write such a line. So V1 is a
      count of CLAIMED owner accepts, not of signed ones.

Predicates (a string of the form `type:arguments`; a path may be absolute, including a Windows
drive such as `C:\\dir\\file.txt`, or relative to the pinned root):
    file_exists:<path>
    file_min_bytes:<path>:<n>
    file_newer_than:<path>:<ISO time>
    regex_in_file:<path>:<regex>
    cmd_exit0:<shell command>              (runs in the pinned root, 300 s timeout)
    owner_accept:<artifact>                (looks for an owner.verdict accept with a non-cli source)
    manual:<text>                          (closed only by `verify --ok <predicate> --evidence <evidence>`)

What a contract stores in the ledger (see README, "What KEEL stores"): the goal text, the
predicates (paths, regexes, shell commands), the evidence of each verify (for `cmd_exit0` the last
300 chars of stdout, for `regex_in_file` up to 80 chars of the match), verdict reasons verbatim.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import schema
from .ledger import Ledger

TRUSTED_VERDICT_SRC = ("tg", "owner", "owner-file")
CONTRACT_EVENTS = ("task.open", "verify", "task.close")


class ContractError(RuntimeError):
    pass


def dod_hash(dod: list[str], root: str | None = None) -> str:
    """Contract hash. With a root it covers the predicates and the absolute project root; without
    one it is the 0.1.0 hash of the predicates alone (kept to read old ledgers)."""
    payload: Any = list(dod) if root is None else {"dod": list(dod), "root": str(root)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _open_hash(opened: dict[str, Any]) -> str:
    d = opened["data"]
    return str(d.get("dod_hash") or dod_hash(d.get("dod") or [], d.get("root")))


def _matches(opened: dict[str, Any], e: dict[str, Any]) -> bool:
    """A verify or close belongs to this open: same contract hash and, when the open has one, same open_id."""
    d = e.get("data") or {}
    if str(d.get("dod_hash")) != _open_hash(opened):
        return False
    oid = opened["data"].get("open_id")
    return oid is None or d.get("open_id") == oid


def _closes(opened: dict[str, Any], e: dict[str, Any]) -> bool:
    """A task.close ends this open unless it names ANOTHER open: a late close of an earlier open
    (its open_id differs) does not end the current one. A close without an open_id (0.1.0) does."""
    oid = opened["data"].get("open_id")
    cid = (e.get("data") or {}).get("open_id")
    return oid is None or cid is None or cid == oid


def replay(events: Iterable[dict[str, Any]]) -> tuple[list[tuple[dict[str, Any], dict[str, Any] | None, bool]], dict[str, dict[str, Any]]]:
    """Replay contract events in ledger order.

    Returns (closes, current):
        closes   [(task.close event, the open it closed or None, proven)] in order; `proven` is True
                 only for outcome verified whose open exists, whose hash/open_id match, and where the
                 LATEST matching verify of every predicate between the open and the close passed;
        current  {task: {"open": task.open event, "last": {predicate: bool}}} for contracts still open.
    Later opens of a task that is already open are ignored; a verify without an open is ignored."""
    current: dict[str, dict[str, Any]] = {}
    closes: list[tuple[dict[str, Any], dict[str, Any] | None, bool]] = []
    for e in events:
        if not isinstance(e, dict) or e.get("ev") not in CONTRACT_EVENTS or e.get("task") is None \
                or not isinstance(e.get("data"), dict):
            continue
        task = str(e["task"])
        if e["ev"] == "task.open":
            current.setdefault(task, {"open": e, "last": {}})
        elif e["ev"] == "verify":
            st = current.get(task)
            if st is not None and _matches(st["open"], e):
                st["last"][str(e["data"].get("predicate"))] = e["data"].get("ok") is True
        else:
            st = current.get(task)
            if st is not None and not _closes(st["open"], e):
                closes.append((e, None, False))  # a stale close of an earlier open: the current open stays
                continue
            current.pop(task, None)
            proven = False
            if st is not None and e["data"].get("outcome") == "verified" and _matches(st["open"], e):
                dod = st["open"]["data"].get("dod") or []
                proven = bool(dod) and all(st["last"].get(str(p)) is True for p in dod)
            closes.append((e, st["open"] if st else None, proven))
    return closes, current


def _state(ledger: Ledger, task: str) -> dict[str, Any] | None:
    _, cur = replay(ledger.iter(where=lambda x: str(x.get("task")) == str(task)))
    return cur.get(str(task))


def find_open(ledger: Ledger, task: str) -> dict[str, Any] | None:
    """The first task.open after the last task.close for the task; later opens are ignored."""
    st = _state(ledger, task)
    return st["open"] if st else None


def _same_dir(a: "str | os.PathLike[str]", b: "str | os.PathLike[str]") -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def open_task(
    ledger: Ledger, *, session: str, project: str, task: str, goal: str, dod: list[str],
    budget: dict[str, Any] | None = None, forecast_p: float | None = None, zone: list[str] | None = None,
    src: str = "cli", agent: str = "head", root: "str | os.PathLike[str] | None" = None,
) -> dict[str, Any]:
    """Open a contract. `root` (default: the current directory) is resolved to an absolute path and
    pinned: predicates always run there."""
    if not dod:
        raise ContractError("a contract without done-predicates is not opened: say how you will prove it")
    for p in dod:
        if ":" not in p or p.split(":", 1)[0] not in PREDICATES:
            raise ContractError(f"unknown predicate {p!r}; allowed: {', '.join(PREDICATES)}")
    if forecast_p is not None and not (0.0 <= forecast_p <= 1.0):
        raise ContractError("forecast_p must be in [0,1]")
    root_abs = str(Path(root if root is not None else os.getcwd()).resolve())
    e = schema.make_event(
        "task.open", session=session, project=project, agent=agent, src=src, task=task,
        data={"goal": goal, "dod": list(dod), "dod_hash": dod_hash(dod, root_abs), "root": root_abs,
              "open_id": os.urandom(12).hex(), "budget": budget or {}, "forecast_p": forecast_p, "zone": zone or []},
    )
    with ledger.lock():  # check and write in one critical section: two opens cannot both pass the check
        if find_open(ledger, task) is not None:
            raise ContractError(f"task {task!r} is already open: predicates do not change after opening; close it and open a new one with another id")
        ledger.append_locked(e)
    return e


# ---- predicates ----------------------------------------------------------------

def _split_path_arg(arg: str) -> tuple[str, str]:
    """`<path>:<argument>`. A Windows drive prefix (`C:\\` or `C:/`) belongs to the path."""
    start = 2 if (len(arg) > 2 and arg[0].isalpha() and arg[1] == ":" and arg[2] in "\\/") else 0
    i = arg.find(":", start)
    if i < 0:
        raise ValueError(f"expected <path>:<argument>, got {arg!r}")
    return arg[:i], arg[i + 1:]


def _resolve(path: str, cwd: Path) -> Path:
    return Path(path) if os.path.isabs(path) else (cwd / path)


def _p_file_exists(arg: str, cwd: Path) -> tuple[bool, str]:
    p = _resolve(arg, cwd)
    return p.exists(), f"{p} exists={p.exists()}"


def _p_file_min_bytes(arg: str, cwd: Path) -> tuple[bool, str]:
    path, n = arg.rsplit(":", 1)
    p = _resolve(path, cwd)
    if not p.exists():
        return False, f"{p} is missing"
    size = p.stat().st_size
    return size >= int(n), f"{p} bytes={size} (need >= {n})"


def _p_file_newer_than(arg: str, cwd: Path) -> tuple[bool, str]:
    path, iso = _split_path_arg(arg)
    p = _resolve(path, cwd)
    if not p.exists():
        return False, f"{p} is missing"
    mtime = datetime.fromtimestamp(p.stat().st_mtime).astimezone()
    ref = schema.parse_ts(iso)
    return mtime >= ref, f"{p} mtime={mtime.isoformat(timespec='seconds')} (need >= {iso})"


def _p_regex_in_file(arg: str, cwd: Path) -> tuple[bool, str]:
    path, rx = _split_path_arg(arg)
    p = _resolve(path, cwd)
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
    for e in ledger.iter(ev="owner.verdict", where=lambda x: isinstance(x.get("data"), dict) and x["data"].get("artifact") == arg
                         and x.get("src") in TRUSTED_VERDICT_SRC):
        last = e
    if last is None:
        return False, f"no owner verdict from a non-cli source for {arg!r} (sources: {', '.join(TRUSTED_VERDICT_SRC)})"
    ok = last["data"].get("verdict") == "accept"
    return ok, f"owner.verdict {last['data'].get('verdict')} at {last['ts']} (src={last.get('src')}, unauthenticated)"


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


def verify(ledger: Ledger, *, task: str, session: str, project: str, cwd: "str | os.PathLike[str] | None" = None,
           src: str = "cli", manual_ok: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Runs every predicate of the open contract in its pinned root, writes verify events, returns them.
    cwd: optional; if given it must be the pinned root (a contract from 0.1.0 without a root runs in cwd).
    manual_ok: {predicate manual:... : evidence} closes manual predicates with an explicit word plus evidence."""
    st = _state(ledger, task)
    if st is None:
        raise ContractError(f"task {task!r} is not open or is already closed")
    opened = st["open"]
    root = opened["data"].get("root")
    if root:
        if cwd is not None and not _same_dir(cwd, root):
            raise ContractError(f"task {task!r} is pinned to {root}; verify runs there, not in {Path(cwd).resolve()}")
        run_dir = Path(root)
    else:
        run_dir = Path(cwd) if cwd is not None else Path.cwd()
    h = _open_hash(opened)
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
                ok, evidence = fn(arg, run_dir, ledger)  # type: ignore[call-arg]
            else:
                ok, evidence = fn(arg, run_dir)
        except Exception as exc:  # noqa: BLE001
            ok, evidence = False, f"check error: {exc}"
        e = schema.make_event("verify", session=session, project=project, src=src, task=task,
                              data={"predicate": pred, "ok": bool(ok), "evidence": evidence, "dod_hash": h,
                                    "open_id": opened["data"].get("open_id"), "root": root})
        ledger.append(e)
        out.append(e)
    return out


def proven_predicates(ledger: Ledger, *, task: str, opened: dict[str, Any] | None = None) -> dict[str, bool]:
    """The latest verify result of each predicate, replayed in ledger order.

    opened=None: the contract open now. opened=<a task.open event of this task> (as returned by
    open_task or find_open): that open, from its line up to the close that ended it, even after the
    task was closed and opened again. An event that cannot be found in the ledger is refused."""
    if opened is None:
        st = _state(ledger, task)
        return dict(st["last"]) if st else {}
    if not isinstance(opened, dict) or opened.get("ev") != "task.open" or not isinstance(opened.get("data"), dict) \
            or str(opened.get("task")) != str(task):
        raise ContractError("opened must be a task.open event of this task, as returned by open_task or find_open")
    oid, h = opened["data"].get("open_id"), opened.get("h")
    if not oid and not h:
        raise ContractError("opened has neither an open_id nor a ledger hash: it cannot be located in the ledger")
    last: dict[str, bool] = {}
    inside = False
    for e in ledger.iter(where=lambda x: str(x.get("task")) == str(task)):
        if not isinstance(e, dict) or not isinstance(e.get("data"), dict):
            continue
        if not inside:
            inside = e.get("ev") == "task.open" and (e["data"].get("open_id") == oid if oid else e.get("h") == h)
            continue
        if e.get("ev") == "verify" and _matches(opened, e):
            last[str(e["data"].get("predicate"))] = e["data"].get("ok") is True
        elif e.get("ev") == "task.close" and _closes(opened, e):
            break
    if not inside:
        raise ContractError("opened is not in this ledger")
    return last


def close_task(ledger: Ledger, *, task: str, outcome: str, session: str, project: str,
               evidence: list[str] | None = None, src: str = "cli") -> dict[str, Any]:
    """Close the open contract. Reading the latest verify state and writing the close are ONE critical
    section under the ledger's append lock: a verify that lands while the close is checked waits and
    is written after the close, where the replay ignores it, so the close and V2 always agree."""
    if outcome not in ("verified", "claimed", "blocked", "abandoned"):
        raise ContractError("outcome: verified | claimed | blocked | abandoned")
    with ledger.lock():
        return _close_locked(ledger, task=task, outcome=outcome, session=session, project=project,
                             evidence=evidence, src=src)


def _close_locked(ledger: Ledger, *, task: str, outcome: str, session: str, project: str,
                  evidence: list[str] | None, src: str) -> dict[str, Any]:
    st = _state(ledger, task)
    if st is None:
        raise ContractError(f"task {task!r} is not open or is already closed")
    opened = st["open"]
    h = _open_hash(opened)
    if outcome == "verified":
        missing = [p for p in opened["data"]["dod"] if st["last"].get(p) is not True]
        if missing:
            raise ContractError("outcome verified is not accepted: predicates not proven by the latest verify of this open: "
                                + "; ".join(missing) + ". Close as claimed or run verify.")
    e = schema.make_event("task.close", session=session, project=project, src=src, task=task,
                          data={"outcome": outcome, "evidence": evidence or [], "forecast_p": opened["data"].get("forecast_p"),
                                "goal": opened["data"].get("goal"), "dod": list(opened["data"]["dod"]), "dod_hash": h,
                                "open_id": opened["data"].get("open_id"), "root": opened["data"].get("root")})
    ledger.append_locked(e)
    return e


def record_verdict(ledger: Ledger, *, session: str, project: str, artifact: str, verdict: str,
                   reasons: list[str], src: str = "cli", claimed_by: str | None = None) -> dict[str, Any]:
    """Record a verdict. The CLI always passes src="cli"; `claimed_by` is free text ("owner", a name)
    recorded as a claim. A non-cli src is for an owner-side integration calling this function."""
    if verdict not in ("accept", "reject"):
        raise ContractError("verdict: accept | reject")
    if verdict == "reject" and not reasons:
        raise ContractError("a reject without reasons is not recorded: reasons verbatim, in the owner's own words")
    data: dict[str, Any] = {"artifact": artifact, "verdict": verdict, "reasons": reasons,
                            "verbatim": src in TRUSTED_VERDICT_SRC, "trusted": src in TRUSTED_VERDICT_SRC}
    if claimed_by:
        data["claimed_by"] = claimed_by
    e = schema.make_event("owner.verdict", session=session, project=project, src=src, data=data)
    ledger.append(e)
    return e
