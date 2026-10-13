# -*- coding: utf-8 -*-
"""Event ledger: append-only, one JSON line per event, never rewritten.

Default location: `<project_root>/.keel/ledger.jsonl`. The hook adapters write to
`~/.keel/<slug>-<hash>/ledger.jsonl` instead (outside the repository). Ledgers built
from history (backfill) are separate files, `data/ledger/<project>.jsonl` by default.

Why a file and not a database: a file survives any session, can be read from any language, can
be diffed by git, and cannot be "quietly zeroed" by one migration command. The ledger is evidence.

Two properties without which the ledger is not evidence:
    * writes are atomic between processes: a file lock for the duration of the append, one
      system `write` call per batch, fsync. Claude Code runs PostToolUse in parallel for several
      calls of one message; without the lock 22 % of events were lost in testing;
    * events are chained by hashes: `prev` = the first 16 hex chars of the previous event's hash,
      `h` = sha256 of (the full previous hash + the canonical event record without h). `audit()`
      detects an edited line and a line inserted, deleted or moved INSIDE the chain.

What the chain does NOT detect on its own: lines cut off the END of the file (the remaining
prefix is still a valid chain), and a file deleted or rebuilt from scratch. For those keep an
external checkpoint (`events` and `last_hash` printed by `python -m keel audit`) somewhere the
agent cannot write, and pass it back with `--expect-count` / `--expect-last-hash`. There is no
signature: the chain catches an edit, not an author.

The ledger is append-only by convention, not by the file system: any process that can write the
file can append to it or rewrite it.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import schema
from ._fast import sha256_hex

TYPE_CHECKING = False  # typing costs 7 ms per import; annotations are deferred (from __future__ import annotations)
if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime
    from typing import Any, Callable, Iterable, Iterator

_O_BINARY = getattr(os, "O_BINARY", 0)


class _FileLock:
    """Inter-process lock through a separate lock file. Windows: msvcrt, otherwise fcntl."""

    def __init__(self, path: "str | os.PathLike[str]", timeout_s: float = 15.0):
        self.path = os.fspath(path)
        self.timeout_s = timeout_s
        self.fd: int | None = None

    def __enter__(self) -> "_FileLock":
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _O_BINARY)
        deadline = time.monotonic() + self.timeout_s
        if sys.platform.startswith("win"):
            import msvcrt
            while True:
                try:
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    return self
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"ledger lock not acquired within {self.timeout_s} s: {self.path}")
                    time.sleep(0.005)
        else:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self

    def __exit__(self, *exc: Any) -> None:
        if self.fd is None:
            return
        try:
            if sys.platform.startswith("win"):
                import msvcrt
                try:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def canonical(event: dict[str, Any]) -> str:
    e = {k: v for k, v in event.items() if k != "h"}
    return json.dumps(e, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def chain_hash(prev_full: str, event: dict[str, Any]) -> str:
    return sha256_hex((prev_full + canonical(event)).encode("utf-8", "replace"))


class Ledger:
    def __init__(self, path: "str | os.PathLike[str]"):
        self.path = os.fspath(path)
        self.bad_lines = 0

    def _lock(self) -> _FileLock:
        return _FileLock(self.path + ".lock")

    def _last_hash(self) -> str:
        """Full hash of the last event; empty for a new ledger. The tail is read backwards in
        growing chunks until a complete line is found, so a record longer than the first chunk
        (64 KiB) does not restart the chain."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return ""
        if size == 0:
            return ""
        chunk = 65536
        with open(self.path, "rb") as fh:
            while True:
                chunk = min(chunk, size)
                fh.seek(size - chunk)
                pieces = fh.read(chunk).split(b"\n")
                if chunk < size:
                    pieces = pieces[1:]  # the first piece may be the end of a longer line
                for ln in reversed(pieces):
                    if not ln.strip():
                        continue
                    try:
                        e = json.loads(ln.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(e, dict) and e.get("h"):
                        return str(e["h"])
                if chunk >= size:
                    return ""
                chunk *= 4

    def _seal(self, event: dict[str, Any], prev_full: str) -> tuple[dict[str, Any], str]:
        schema.validate(event)
        e = dict(event)
        e.pop("h", None)
        e["prev"] = prev_full[:16]
        h = chain_hash(prev_full, e)
        e["h"] = h
        return e, h

    # ---- writing ------------------------------------------------------------
    def lock(self) -> _FileLock:
        """The append lock. Hold it to make "read the ledger, check, append" one critical section,
        and append with `append_locked` inside it (taking the lock twice would wait for itself)."""
        return self._lock()

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock():
            return self.append_locked(event)

    def append_locked(self, event: dict[str, Any]) -> dict[str, Any]:
        """Append while the caller holds `lock()`."""
        prev = self._last_hash()
        e, h = self._seal(event, prev)
        self._write_bytes((schema.dumps(e) + "\n").encode("utf-8"))
        event["h"] = h
        event["prev"] = e["prev"]
        return event

    def append_many(self, events: Iterable[dict[str, Any]]) -> int:
        n = 0
        with self._lock():
            prev = self._last_hash()
            buf = bytearray()
            for ev in events:
                e, h = self._seal(ev, prev)
                buf += (schema.dumps(e) + "\n").encode("utf-8")
                prev = h
                n += 1
                if len(buf) > 4 * 1024 * 1024:
                    self._write_bytes(bytes(buf))
                    buf = bytearray()
            if buf:
                self._write_bytes(bytes(buf))
        return n

    def _write_bytes(self, data: bytes) -> None:
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o644)
        try:
            os.write(fd, data)
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)

    # ---- reading ------------------------------------------------------------
    def exists(self) -> bool:
        try:
            return os.path.getsize(self.path) > 0
        except OSError:
            return False

    def iter(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        ev: str | set[str] | None = None,
        session: str | None = None,
        where: Callable[[dict[str, Any]], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Streaming read with filters. Corrupt lines are skipped and counted
        (see `bad_lines`) instead of crashing the read: the ledger must always be readable."""
        self.bad_lines = 0
        if not os.path.exists(self.path):
            return
        evset = {ev} if isinstance(ev, str) else ev
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    self.bad_lines += 1
                    continue
                if not isinstance(e, dict):
                    self.bad_lines += 1
                    continue
                if evset and e.get("ev") not in evset:
                    continue
                if session and e.get("session") != session:
                    continue
                if since or until:
                    try:
                        t = schema.parse_ts(e.get("ts", ""))
                    except Exception:  # noqa: BLE001
                        self.bad_lines += 1
                        continue
                    if since and t < since:
                        continue
                    if until and t > until:
                        continue
                if where and not where(e):
                    continue
                yield e

    def all(self, **kw: Any) -> list[dict[str, Any]]:
        return list(self.iter(**kw))

    def sessions(self) -> list[str]:
        seen: dict[str, None] = {}
        for e in self.iter():
            seen.setdefault(str(e.get("session")), None)
        return list(seen)

    # ---- integrity ----------------------------------------------------------
    def audit(self, *, max_breaks: int = 20, expect_count: int | None = None,
              expect_last_hash: str | None = None) -> dict[str, Any]:
        """Check the hash chain. Returns the number of events, breaks, the first breaks (line number
        and reason) and `last_hash` (record it with `events` as an external checkpoint).

        Detected: an edited line; a line inserted, deleted or moved inside the chain; a corrupt line;
        a line without a hash (old format); a missing file. NOT detected without a checkpoint: lines
        cut off the end, a file rebuilt from scratch. With `expect_count` (and optionally
        `expect_last_hash`, the full hash or a prefix of at least 16 chars) a shorter file, or a
        chain whose hash at that position differs, is a break."""
        total = 0
        breaks: list[dict[str, Any]] = []
        prev_full = ""
        hash_at_expected: str | None = None
        if not os.path.exists(self.path):
            return {"events": 0, "breaks": 1, "ok": False, "missing": True, "last_hash": "",
                    "details": [{"line": 0, "why": "ledger file is missing"}]}
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for ln, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    breaks.append({"line": ln, "why": "corrupt line"})
                    continue
                if not isinstance(e, dict):
                    breaks.append({"line": ln, "why": "corrupt line: not a JSON object"})
                    continue
                h = e.get("h")
                if not h or not isinstance(h, str):
                    breaks.append({"line": ln, "why": "no hash"})
                    prev_full = ""
                    continue
                if e.get("prev") != prev_full[:16]:
                    breaks.append({"line": ln, "why": "prev mismatch: insertion, deletion or reorder"})
                expected = chain_hash(prev_full, e)
                if expected != h:
                    breaks.append({"line": ln, "why": "hash mismatch: line was edited"})
                prev_full = h
                if expect_count is not None and total == expect_count:
                    hash_at_expected = h
                if len(breaks) > max_breaks:
                    break
        if expect_count is not None and total < expect_count:
            breaks.append({"line": 0, "why": f"tail truncated or file replaced: {total} events, the checkpoint says {expect_count}"})
        if expect_last_hash:
            target = hash_at_expected if expect_count is not None else prev_full
            if len(expect_last_hash) < 16 or not (target or "").startswith(expect_last_hash):
                breaks.append({"line": 0, "why": "checkpoint hash mismatch: the chain up to the checkpoint was cut or rebuilt"})
        return {"events": total, "breaks": len(breaks), "ok": not breaks, "last_hash": prev_full,
                "details": breaks[:max_breaks]}


# Fields that can hold user text, paths, commands or command output, per event type.
# Used by `redact_event` (python -m keel export). Numbers, booleans and hashes are kept.
REDACT_FIELDS: dict[str, tuple[str, ...]] = {
    "task.open": ("goal", "dod", "zone", "root"),
    "task.close": ("goal", "dod", "evidence", "root"),
    "verify": ("predicate", "evidence", "root"),
    "owner.verdict": ("artifact", "reasons", "claimed_by"),
    "incident": ("detail",),
    "tool.call": ("path",),
    "session.start": ("cwd", "transcript"),
}


def _mask(v: Any) -> str:
    return "redacted:" + sha256_hex(json.dumps(v, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace"))[:12]


def redact_event(event: dict[str, Any]) -> dict[str, Any]:
    """A copy of the event with text-bearing fields replaced by `redacted:<hash>` (equal inputs
    give equal masks). Numbers and booleans inside an incident detail are kept. The copy keeps
    the original `h`/`prev`, so a redacted export no longer passes `audit`."""
    out = dict(event)
    data = dict(event.get("data") or {}) if isinstance(event.get("data"), dict) else {}
    for k in REDACT_FIELDS.get(str(event.get("ev")), ()):
        v = data.get(k)
        if v in (None, "", [], {}):
            continue
        if k == "detail" and isinstance(v, dict):
            data[k] = {dk: (dv if dv is None or isinstance(dv, (bool, int, float)) else _mask(dv)) for dk, dv in v.items()}
        else:
            data[k] = _mask(v)
    out["data"] = data
    out["redacted"] = True
    return out


def window_since(days: float) -> "datetime":
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) - timedelta(days=days)


def default_ledger_path(project_root: "str | os.PathLike[str]") -> str:
    return os.path.join(os.fspath(project_root), ".keel", "ledger.jsonl")
