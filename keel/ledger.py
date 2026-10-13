# -*- coding: utf-8 -*-
"""Event ledger: append-only, one JSON line per event, never rewritten.

Default location: `<project_root>/.keel/ledger.jsonl`. The hook adapters write to
`~/.keel/<encoded launch dir>/ledger.jsonl` instead (outside the repository). Ledgers built
from history (backfill) are separate files, `data/ledger/<project>.jsonl` by default.

Why a file and not a database: a file survives any session, can be read from any language, can
be diffed by git, and cannot be "quietly zeroed" by one migration command. The ledger is evidence.

Two properties without which the ledger is not evidence:
    * writes are atomic between processes: a file lock for the duration of the append, one
      system `write` call per batch, fsync. Claude Code runs PostToolUse in parallel for several
      calls of one message; without the lock 22 % of events were lost in testing;
    * events are chained by hashes: `prev` = the first 16 hex chars of the previous event's hash,
      `h` = sha256 of (the full previous hash + the canonical event record without h). `audit()`
      finds any insertion, deletion or edit of a line. There is no signature: the chain catches
      an edit, not an author.
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
        """Full hash of the last event (the file tail is read); empty for a new ledger."""
        try:
            if os.path.getsize(self.path) == 0:
                return ""
        except OSError:
            return ""
        with open(self.path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            chunk = min(size, 65536)
            fh.seek(size - chunk)
            tail = fh.read(chunk)
        lines = [ln for ln in tail.split(b"\n") if ln.strip()]
        for ln in reversed(lines):
            try:
                e = json.loads(ln.decode("utf-8", "replace"))
                if isinstance(e, dict) and e.get("h"):
                    return str(e["h"])
            except json.JSONDecodeError:
                continue
        return ""

    def _seal(self, event: dict[str, Any], prev_full: str) -> tuple[dict[str, Any], str]:
        schema.validate(event)
        e = dict(event)
        e.pop("h", None)
        e["prev"] = prev_full[:16]
        h = chain_hash(prev_full, e)
        e["h"] = h
        return e, h

    # ---- writing ------------------------------------------------------------
    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock():
            prev = self._last_hash()
            e, h = self._seal(event, prev)
            line = (schema.dumps(e) + "\n").encode("utf-8")
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY, 0o644)
            try:
                os.write(fd, line)
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
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
    def audit(self, *, max_breaks: int = 20) -> dict[str, Any]:
        """Check the hash chain. Returns the number of events, breaks, and the first breaks
        (line number and reason). An event without h (old-format ledger) counts as a break."""
        total = 0
        breaks: list[dict[str, Any]] = []
        prev_full = ""
        if not os.path.exists(self.path):
            return {"events": 0, "breaks": 0, "ok": True, "details": []}
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
                h = e.get("h")
                if not h:
                    breaks.append({"line": ln, "why": "no hash"})
                    prev_full = ""
                    continue
                if e.get("prev") != prev_full[:16]:
                    breaks.append({"line": ln, "why": "prev mismatch: insertion, deletion or reorder"})
                expected = chain_hash(prev_full, e)
                if expected != h:
                    breaks.append({"line": ln, "why": "hash mismatch: line was edited"})
                prev_full = h
                if len(breaks) > max_breaks:
                    break
        return {"events": total, "breaks": len(breaks), "ok": not breaks, "details": breaks[:max_breaks]}


def window_since(days: float) -> "datetime":
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) - timedelta(days=days)


def default_ledger_path(project_root: "str | os.PathLike[str]") -> str:
    return os.path.join(os.fspath(project_root), ".keel", "ledger.jsonl")
