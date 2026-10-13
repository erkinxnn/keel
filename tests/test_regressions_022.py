# -*- coding: utf-8 -*-
"""Regression tests for the 0.2.2 fixes (third independent review). Each test failed on 0.2.1.

The number in a test name is the item of the 0.2.2 change list (see CHANGELOG.md)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "keel" / "hooks"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HOOKS))

from keel import schema  # noqa: E402
from keel.__main__ import main as keel_main  # noqa: E402
from keel.ledger import Ledger  # noqa: E402

import _common  # noqa: E402
import stop  # noqa: E402


# ---- helpers ---------------------------------------------------------------------

def _load_installer():
    spec = importlib.util.spec_from_file_location("keel_install_hooks_022", ROOT / "tools" / "install_hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def hook_env(tmp_path, monkeypatch):
    led = tmp_path / "ledger.jsonl"
    cwd = tmp_path / "proj"
    cwd.mkdir()
    monkeypatch.setenv("KEEL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KEEL_LEDGER", str(led))
    _common._QUEUE.clear()
    _common._DIRS.clear()
    yield {"cwd": str(cwd), "ledger": Ledger(led), "tmp": tmp_path}
    _common._QUEUE.clear()
    _common._DIRS.clear()


def _run(name, fn, payload) -> str:
    """Run an adapter in-process exactly as Claude Code would: JSON on stdin, answer on stdout."""
    old_in, old_out = sys.stdin, sys.stdout
    out = io.StringIO()
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), out
    try:
        _common.run_hook(name, fn)
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    return out.getvalue()


def _fill(path: Path, n: int = 3) -> Ledger:
    led = Ledger(path)
    for i in range(n):
        led.append(schema.make_event("claim", session="s", project="p", data={"kind": "done", "n": i}))
    return led


def _is_event(data) -> bool:
    """True for the bytes of a ledger line (a JSON object); other writes (locks) pass through."""
    return bytes(data[:1]) == b"{"


def _link_or_skip(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard links are not supported on this file system")


# ---- 1. export never opens a predictable temporary name ---------------------------------

def test_1_export_ignores_a_planted_temp_name_that_links_to_the_source(tmp_path):
    src = tmp_path / "ledger.jsonl"
    _fill(src)
    before = src.read_bytes()
    out = tmp_path / "share.jsonl"
    planted = tmp_path / f".share.jsonl.keel_tmp_{os.getpid()}"  # the name 0.2.1 used for its temporary file
    _link_or_skip(src, planted)
    assert keel_main(["export", "--ledger", str(src), "--out", str(out)]) == 0
    assert src.read_bytes() == before, "the source ledger was modified through the planted link"
    assert Ledger(src).audit()["ok"]
    assert len(out.read_text(encoding="utf-8").splitlines()) == 3
    assert planted.read_bytes() == before  # the planted path was never opened for writing


def test_1_export_ignores_a_planted_symlink(tmp_path):
    src = tmp_path / "ledger.jsonl"
    _fill(src)
    before = src.read_bytes()
    planted = tmp_path / f".share.jsonl.keel_tmp_{os.getpid()}"
    try:
        os.symlink(src, planted)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("symlinks are not available here")
    assert keel_main(["export", "--ledger", str(src), "--out", str(tmp_path / "share.jsonl")]) == 0
    assert src.read_bytes() == before


def test_1_export_leaves_no_temporary_file(tmp_path):
    src = tmp_path / "ledger.jsonl"
    _fill(src, 2)
    assert keel_main(["export", "--ledger", str(src), "--out", str(tmp_path / "share.jsonl")]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ledger.jsonl", "ledger.jsonl.lock", "share.jsonl"]


# ---- 2. the ledger write is complete or it raises ----------------------------------------

def test_2_short_writes_are_continued_until_every_byte_is_in(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    real = os.write

    def short(fd, data):
        if not _is_event(data):
            return real(fd, data)
        return real(fd, bytes(data)[:7])  # the kernel accepts 7 bytes per call

    with monkeypatch.context() as m:
        m.setattr(os, "write", short)
        led.append(schema.make_event("claim", session="s", project="p", data={"kind": "done", "n": 1}))
        led.append_many([schema.make_event("claim", session="s", project="p", data={"kind": "done", "n": i}) for i in (2, 3)])
    r = Ledger(path).audit()
    assert r["ok"] and r["events"] == 3, r
    assert Ledger(path).all() and Ledger(path).bad_lines == 0


def test_2_a_zero_byte_write_raises_instead_of_looping_or_succeeding(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "ledger.jsonl")
    real = os.write
    with monkeypatch.context() as m:
        m.setattr(os, "write", lambda fd, data: 0 if _is_event(data) else real(fd, data))
        with pytest.raises(OSError):
            led.append(schema.make_event("claim", session="s", project="p", data={"kind": "done"}))


def test_2_an_fsync_error_is_not_swallowed(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "ledger.jsonl")

    def boom(fd):
        raise OSError("simulated: fsync failed")

    with monkeypatch.context() as m:
        m.setattr(os, "fsync", boom)
        with pytest.raises(OSError):
            led.append(schema.make_event("claim", session="s", project="p", data={"kind": "done"}))
        with pytest.raises(OSError):
            led.append_many([schema.make_event("claim", session="s", project="p", data={"kind": "done"})])


def test_2_a_failure_part_way_leaves_the_next_append_on_a_fresh_line(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    led = _fill(path, 1)
    real = os.write
    calls = {"n": 0}

    def flaky(fd, data):
        if calls["n"] == 0:
            if not _is_event(data):
                return real(fd, data)
            calls["n"] = 1
            return real(fd, bytes(data)[:10])  # a short write: 10 bytes of the line are in the file
        if bytes(data) == b"\n":
            return real(fd, data)  # the repair of the partial line
        raise OSError("simulated: disk full")  # the rest of the line is refused

    with monkeypatch.context() as m:
        m.setattr(os, "write", flaky)
        with pytest.raises(OSError):
            led.append(schema.make_event("claim", session="s", project="p", data={"kind": "done", "n": 9}))
    assert path.read_bytes().endswith(b"\n")  # the partial line was terminated
    led.append(schema.make_event("claim", session="s", project="p", data={"kind": "done", "n": 10}))
    events = Ledger(path).all()
    assert [e["data"]["n"] for e in events] == [0, 10]  # the new event is not glued to the garbage


# ---- 3. the installer needs the adapter as the script, with no -c / -m in the options -------

def test_3_inline_code_with_combined_flags_is_not_a_keel_hook(tmp_path):
    inst = _load_installer()
    adapter = str(inst.HOOKS / "stop.py")
    py = sys.executable
    foreign = [
        f'"{py}" -Scprint(123) "{adapter}"',
        f'"{py}" -Sc "print(123)" "{adapter}"',
        f'"{py}" -Em keel "{adapter}"',
        f'"{py}" -S -c "print(1)" "{adapter}"',
        f'"{py}" -m keel "{adapter}"',
        f'"{py}" -W ignore -Sc "pass" "{adapter}"',
        f'"{py}" -Xutf8 -c "pass" "{adapter}"',
    ]
    for cmd in foreign:
        assert not inst._is_keel_hook({"type": "command", "command": cmd}), cmd
    own = [
        f'"{py}" -S -E "{adapter}"',
        f'"{py}" -SE "{adapter}"',
        f'"{py}" -W ignore -S "{adapter}"',
        f'"{py}" -Wignore "{adapter}"',
        f'"{py}" -X utf8 "{adapter}"',
        f'"{py}" -Xutf8 -B "{adapter}"',
    ]
    for cmd in own:
        assert inst._is_keel_hook({"type": "command", "command": cmd}), cmd


def test_3_uninstall_keeps_the_inline_code_hook(tmp_path):
    inst = _load_installer()
    adapter = str(inst.HOOKS / "stop.py")
    py = sys.executable
    keel_cmd = f'"{py}" -S -E "{adapter}"'
    inline_cmd = f'"{py}" -Scprint(123) "{adapter}"'
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    hooks = [{"type": "command", "command": c} for c in (keel_cmd, inline_cmd)]
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": hooks}]}}), encoding="utf-8")
    inst.install(settings, remove=True)
    left = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"]
    assert [h["command"] for h in left] == [inline_cmd]


# ---- 4. report refuses to overwrite a ledger and writes atomically --------------------------

def test_4_report_refuses_out_equal_to_the_ledger(tmp_path, capsys):
    src = tmp_path / "ledger.jsonl"
    _fill(src)
    before = src.read_bytes()
    rc = keel_main(["report", "--ledger", str(src), "--out", str(src)])
    assert rc != 0
    assert "REFUSED" in capsys.readouterr().out
    assert src.read_bytes() == before


def test_4_report_refuses_a_link_to_the_ledger(tmp_path):
    src = tmp_path / "ledger.jsonl"
    _fill(src)
    before = src.read_bytes()
    alias = tmp_path / "report.md"
    _link_or_skip(src, alias)
    assert keel_main(["report", "--ledger", str(src), "--out", str(alias)]) != 0
    assert src.read_bytes() == before and alias.read_bytes() == before


def test_4_report_on_a_directory_refuses_one_of_its_ledgers(tmp_path):
    d = tmp_path / "ledgers"
    d.mkdir()
    a = d / "a.jsonl"
    _fill(a)
    before = a.read_bytes()
    assert keel_main(["report", "--ledger", str(d), "--out", str(a)]) != 0
    assert a.read_bytes() == before


def test_4_report_is_written_atomically_and_leaves_no_temporary_file(tmp_path):
    src = tmp_path / "ledger.jsonl"
    _fill(src)
    before = src.read_bytes()
    out = tmp_path / "report.md"
    out.write_text("stale\n", encoding="utf-8")
    planted = tmp_path / f".report.md.keel_tmp_{os.getpid()}"
    _link_or_skip(src, planted)
    assert keel_main(["report", "--ledger", str(src), "--out", str(out)]) == 0
    assert src.read_bytes() == before  # a planted temp name is not used for the write
    assert "stale" not in out.read_text(encoding="utf-8")
    planted.unlink()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ledger.jsonl", "ledger.jsonl.lock", "report.md"]


# ---- 5. Stop marks a request as seen only after its usage is written -------------------------

def _stop_transcript(path: Path) -> None:
    rows = [
        {"type": "user", "timestamp": "2026-09-02T10:00:00Z", "message": {"role": "user", "content": "please check the build"}},
        {"type": "assistant", "timestamp": "2026-09-02T10:00:01Z", "requestId": "q1",
         "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 5, "output_tokens": 2},
                     "content": [{"type": "text", "text": "looking"}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _usage_rows(led: Ledger, req: str) -> list:
    return [e for e in led.all(ev="usage") if (e.get("data") or {}).get("req") == req]


def test_5_stop_write_failure_does_not_lose_the_usage_forever(hook_env, monkeypatch):
    cwd = hook_env["cwd"]
    tr = hook_env["tmp"] / "main.jsonl"
    _stop_transcript(tr)
    payload = {"session_id": "s", "cwd": cwd, "transcript_path": str(tr), "hook_event_name": "Stop"}
    real = Ledger.append_many

    def boom(self, events):
        raise OSError("simulated: disk full")

    monkeypatch.setattr(Ledger, "append_many", boom)
    _run("stop", stop.main, payload)
    monkeypatch.setattr(Ledger, "append_many", real)
    assert "q1" not in (_common.load_state(cwd, "s").get("seen_req") or [])
    assert _usage_rows(hook_env["ledger"], "q1") == []
    _run("stop", stop.main, payload)  # the retry writes the usage
    assert len(_usage_rows(hook_env["ledger"], "q1")) == 1
    assert "q1" in _common.load_state(cwd, "s").get("seen_req", [])
    _run("stop", stop.main, payload)  # and a third call does not write it again
    assert len(_usage_rows(hook_env["ledger"], "q1")) == 1
