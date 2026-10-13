# -*- coding: utf-8 -*-
"""Shared code of the Claude Code hook adapters.

Principles (derived from audits of a real multi-hook setup):
    * python, not pythonw: a traceback is not swallowed but written to a side log `hooks.log`;
    * fail-open by default: any adapter error = exit code 0 and a line in the log, the turn does
      not fail; the only exception is set by the environment variable KEEL_STRICT=1 and applies
      only to Stop (a claim without evidence is blocked with a reason, at most once per turn);
    * stdin and stdout are explicitly utf-8 (a class of defect: a legacy code page turns
      non-ASCII letters into dashes);
    * session state changes only under an inter-process lock and is written through a unique
      temporary file (a shared .tmp gave PermissionError and lost writes);
    * the adapter measures its own time and writes a layer.cost event: the layer must know its
      own price;
    * no import heavier than the standard library, no network call.

Import cost (measured with `python -S -E -X importtime`): `pathlib` 21 ms, `re` 24 ms,
`traceback` 23 ms, `uuid` 9 ms, `typing` 7 ms on EVERY call. So only `json`, `os`, `sys`, `time`
and `contextlib` are imported here: paths go through `os.path`, a temporary file name is made
with `os.urandom`, and `traceback` is imported inside the error handler, i.e. once per failure,
not once per call.

Events are queued and go into the ledger as ONE write (`flush`): one lock and one fsync per
adapter call instead of two or three.

Ledger location: the KEEL_LEDGER variable, otherwise `~/.keel/<slug>-<hash>/ledger.jsonl`, where
<slug> is the launch directory with every non-alphanumeric character turned into a dash (readable)
and <hash> is 10 hex chars of the SHA-256 of the normalised absolute path (so `a_b` and `a-b` never
share a folder). A folder written by 0.1.0/0.2.0 under the bare slug is still used when its
project.json names the same directory. `KEEL_HOME` overrides `~/.keel`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager

TYPE_CHECKING = False  # typing costs 7 ms per import; annotations are deferred (PEP 563)
if TYPE_CHECKING:  # pragma: no cover
    from typing import Any, Callable, Iterator

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stdin, "reconfigure"):
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from keel import schema  # noqa: E402
from keel._fast import sha256_hex  # noqa: E402
from keel.ledger import Ledger, _FileLock  # noqa: E402

_DIRS: dict[str, str] = {}
_QUEUE: list[dict] = []
_RUN: dict = {}  # the adapter call in progress: name, start time, session, cwd, whether layer.cost was queued


def read_input() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def keel_home() -> str:
    env = os.environ.get("KEEL_HOME")
    if env:
        return env
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".keel")


def keel_dir(cwd: str | None) -> str:
    """The ledger and state live OUTSIDE the project repository: `~/.keel/<slug>-<hash>/`
    (the way Claude Code stores its own transcripts). Otherwise the ledger would land in git
    auto-sync and in the session's context. A project.json with the real directory path is
    written next to it. The directory is computed once per process: mkdir and stat on every
    call cost money too."""
    key = cwd or ""
    hit = _DIRS.get(key)
    if hit is not None:
        return hit
    base = os.path.abspath(cwd) if cwd else os.getcwd()
    home = keel_home()
    d = os.path.join(home, project_dir_name(base))
    if not os.path.isdir(d):
        old = os.path.join(home, encode_project_dir(base))
        if _owned_by(old, base):
            d = old  # a folder written by 0.1.0/0.2.0 for this very directory
    os.makedirs(d, exist_ok=True)
    marker = os.path.join(d, "project.json")
    if not os.path.exists(marker):
        try:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"cwd": base}, ensure_ascii=False))
        except OSError:
            pass
    _DIRS[key] = d
    return d


def project_dir_name(base: str) -> str:
    """Folder name for a launch directory: readable slug + hash of the normalised absolute path."""
    key = sha256_hex(os.path.normcase(os.path.abspath(base)).encode("utf-8", "surrogatepass"))[:10]
    return f"{encode_project_dir(base)[-80:]}-{key}"


def _owned_by(d: str, base: str) -> bool:
    try:
        with open(os.path.join(d, "project.json"), "r", encoding="utf-8") as fh:
            owner = json.load(fh).get("cwd")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(owner, str) and os.path.normcase(os.path.abspath(owner)) == os.path.normcase(base)


def ledger_for(cwd: str | None) -> Ledger:
    env = os.environ.get("KEEL_LEDGER")
    if env:
        return Ledger(env)
    return Ledger(os.path.join(keel_dir(cwd), "ledger.jsonl"))


def emit(event: dict) -> dict:
    """Queue an event of this call. The queue goes into the ledger as one write."""
    _QUEUE.append(event)
    return event


def flush(cwd: str | None) -> None:
    if not _QUEUE:
        return
    batch = list(_QUEUE)
    _QUEUE.clear()
    ledger_for(cwd).append_many(batch)


def _cost_event(out: "dict | None", failed: bool) -> dict:
    injected = len(json.dumps(out, ensure_ascii=False)) if out else 0
    ms = int((time.perf_counter() - _RUN.get("t0", time.perf_counter())) * 1000)
    return schema.make_event(
        "layer.cost", session=str(_RUN.get("session") or "nosession"), project=project_name(_RUN.get("cwd")), src="hook",
        data={"hook": _RUN.get("name") or "?", "ms": ms, "injected_bytes": injected, "failed": failed})


def flush_call(cwd: str | None, out: "dict | None" = None) -> None:
    """Write the queued events of this call NOW, together with its layer.cost (measured up to this
    point), in one write. Raises if the write fails, so a caller marks something as written only
    after this returns. run_hook then writes no second layer.cost."""
    if _RUN and not _RUN.get("cost_done"):
        _RUN["cost_done"] = True
        emit(_cost_event(out, False))
    flush(cwd)


def project_name(cwd: str | None) -> str:
    if not cwd:
        return os.path.basename(os.getcwd())
    return os.path.basename(cwd.rstrip("\\/")) or cwd


def encode_project_dir(cwd: str) -> str:
    """The way Claude Code encodes a launch directory into the name of its memory folder: any
    character that is not an ASCII letter or digit becomes a dash
    (for example C:\\Users\\me\\proj -> C--Users-me-proj)."""
    return "".join(c if ("0" <= c <= "9" or "a" <= c <= "z" or "A" <= c <= "Z") else "-" for c in cwd)


def agent_label(inp: dict) -> str:
    """Head or subagent: Claude Code passes agent_id in the common input fields of a hook."""
    aid = inp.get("agent_id")
    return f"sub:{aid}" if aid else "head"


def memory_attached(cwd: str | None) -> bool | None:
    if not cwd:
        return None
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.exists(os.path.join(home, ".claude", "projects", encode_project_dir(cwd), "memory", "MEMORY.md"))


def state_path(cwd: str | None, session: str) -> str:
    d = os.path.join(keel_dir(cwd), "state")
    os.makedirs(d, exist_ok=True)
    safe = "".join(c if (c.isascii() and (c.isalnum() or c in "_.-")) else "_" for c in (session or "nosession"))
    return os.path.join(d, f"{safe}.json")


def load_state(cwd: str | None, session: str) -> dict:
    p = state_path(cwd, session)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state_unlocked(p: str, st) -> None:
    tmp = f"{p}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(st, ensure_ascii=False))
    os.replace(tmp, p)


@contextmanager
def locked_state(cwd: str | None, session: str) -> "Iterator[dict]":
    """Read-modify-write of the session state, atomic between parallel adapters."""
    p = state_path(cwd, session)
    with _FileLock(p + ".lock"):
        st: dict = {}
        try:
            with open(p, "r", encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        yield st
        _save_state_unlocked(p, st)


def known_classes(cwd: str | None, ledger: Ledger) -> set:
    """Incident classes that already happened in the project: a small cache `state/classes.json`;
    the ledger is read whole only once, on first use."""
    d = os.path.join(keel_dir(cwd), "state")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "classes.json")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return set(json.load(fh))
    except (OSError, ValueError):
        pass
    classes = {str(e["data"].get("cls")) for e in ledger.iter(ev="incident")}
    with _FileLock(p + ".lock"):
        _save_state_unlocked(p, sorted(classes))
    return classes


def remember_classes(cwd: str | None, classes: set) -> None:
    p = os.path.join(keel_dir(cwd), "state", "classes.json")
    with _FileLock(p + ".lock"):
        cur: set = set()
        try:
            with open(p, "r", encoding="utf-8") as fh:
                cur = set(json.load(fh))
        except (OSError, ValueError):
            cur = set()
        _save_state_unlocked(p, sorted(cur | set(classes)))


def log(cwd: str | None, msg: str) -> None:
    try:
        with open(os.path.join(keel_dir(cwd), "hooks.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{schema.now_iso()} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def run_hook(name: str, fn: "Callable[[dict], dict | None]") -> int:
    """Wrapper: reads the input, calls fn, prints the JSON answer (if any), writes layer.cost.
    Never fails outwards; in strict mode only Stop may return a block.
    All events of the call (including layer.cost) go into the ledger as one write."""
    t0 = time.perf_counter()
    _RUN.clear()
    _RUN.update({"name": name, "t0": t0})
    inp: dict = {}
    cwd = None
    out: dict | None = None
    failed = False
    try:
        inp = read_input()
        cwd = inp.get("cwd")
        _RUN.update({"session": inp.get("session_id"), "cwd": cwd})
        out = fn(inp) or {}
    except Exception:  # noqa: BLE001
        import traceback
        failed = True
        log(cwd, f"{name}: ADAPTER ERROR (fail-open)\n{traceback.format_exc()}")
    try:
        if not _RUN.get("cost_done"):
            _RUN["cost_done"] = True
            emit(_cost_event(out, failed))
        flush(cwd)
    except Exception as exc:  # noqa: BLE001
        log(cwd, f"{name}: events of the call were not written: {exc}")
    if out and not failed:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0
