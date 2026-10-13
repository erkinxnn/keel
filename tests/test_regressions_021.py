# -*- coding: utf-8 -*-
"""Regression tests for the 0.2.1 fixes (second independent review). Each test failed on 0.2.0.

The letter in a test name is the item of the 0.2.1 change list (see CHANGELOG.md)."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "keel" / "hooks"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HOOKS))

from keel import contract, incidents, schema, scoreboard, vitals  # noqa: E402
from keel.__main__ import main as keel_main  # noqa: E402
from keel.extract import norm_path  # noqa: E402
from keel.ledger import Ledger  # noqa: E402

import _common  # noqa: E402
import post_tool_use  # noqa: E402
import subagent_stop  # noqa: E402
import user_prompt_submit  # noqa: E402


# ---- helpers ---------------------------------------------------------------------

def _load_installer():
    spec = importlib.util.spec_from_file_location("keel_install_hooks", ROOT / "tools" / "install_hooks.py")
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


def _run(name, fn, payload, monkeypatch=None) -> str:
    """Run an adapter in-process exactly as Claude Code would: JSON on stdin, answer on stdout."""
    old_in, old_out = sys.stdin, sys.stdout
    out = io.StringIO()
    sys.stdin, sys.stdout = io.StringIO(json.dumps(payload)), out
    try:
        _common.run_hook(name, fn)
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    return out.getvalue()


def _call(tuid, ok, ts="2026-09-02T10:00:00+00:00", **extra):
    d = {"tool": "Bash", "ok": ok, "ms": 1, "in_bytes": 1, "out_bytes": 1, "sig": "x", "tool_use_id": tuid}
    d.update(extra)
    return schema.make_event("tool.call", session="s", project="p", ts=ts, data=d)


def _sub_transcript(path: Path, tuid: str) -> None:
    rows = [
        {"type": "user", "timestamp": "2026-09-02T10:03:00Z", "isSidechain": True, "message": {"role": "user", "content": "task"}},
        {"type": "assistant", "timestamp": "2026-09-02T10:03:01Z", "isSidechain": True, "requestId": "q-" + tuid,
         "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 5, "output_tokens": 1},
                     "content": [{"type": "tool_use", "id": tuid, "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "timestamp": "2026-09-02T10:03:02Z", "isSidechain": True,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tuid, "content": "a"}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _rows(led: Ledger, tuid: str) -> list:
    return [e for e in led.iter(ev="tool.call") if e["data"].get("tool_use_id") == tuid]


def _open_verify(led, root, task="t", dod=("manual:a",)):
    contract.open_task(led, session="s", project="p", task=task, goal="g", dod=list(dod), root=root)
    contract.verify(led, task=task, session="s", project="p", manual_ok={p: "seen" for p in dod})


def _record(cwd, tool, path, tuid, agent=None):
    tin = {"file_path": path}
    if tool == "Edit":
        tin.update({"old_string": "a", "new_string": "b"})
    inp = {"session_id": "s", "cwd": cwd, "tool_name": tool, "tool_input": tin, "tool_use_id": tuid}
    if agent:
        inp["agent_id"] = agent
    return post_tool_use.record(inp, ok=True, out_bytes=500 if tool == "Read" else 10)


# ---- a. export ---------------------------------------------------------------------

def test_a_export_refuses_a_hard_link_to_the_source(tmp_path):
    src = tmp_path / "ledger.jsonl"
    led = Ledger(src)
    for i in range(3):
        led.append(schema.make_event("claim", session="s", project="p", data={"kind": "done", "n": i}))
    before = src.read_bytes()
    alias = tmp_path / "alias.jsonl"
    try:
        os.link(src, alias)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard links are not supported on this file system")
    assert keel_main(["export", "--ledger", str(src), "--out", str(alias)]) == 2
    assert src.read_bytes() == before


def test_a_export_replaces_the_output_atomically(tmp_path):
    src = tmp_path / "ledger.jsonl"
    Ledger(src).append(schema.make_event("claim", session="s", project="p", data={"kind": "done"}))
    out = tmp_path / "share.jsonl"
    out.write_text("stale\n", encoding="utf-8")
    assert keel_main(["export", "--ledger", str(src), "--out", str(out)]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ledger.jsonl", "ledger.jsonl.lock", "share.jsonl"]


# ---- b. de-duplication keeps the finished record ----------------------------------

def test_b_dedup_prefers_the_completed_record():
    r = vitals.compute([_call("X", None), _call("X", False, ts="2026-09-02T10:00:01+00:00")])
    x = r["vitals"]["V6_tool_error_rate"]["extra"]
    assert (x["errors"], x["unfinished"], x["calls"]) == (1, 0, 1)


# ---- c. seen only after a successful write -----------------------------------------

def test_c_failed_write_does_not_mark_the_call_seen(hook_env, monkeypatch):
    cwd = hook_env["cwd"]
    real = Ledger.append_many

    def boom(self, events):
        raise OSError("simulated: disk full")

    monkeypatch.setattr(Ledger, "append_many", boom)
    _run("post_tool_use", post_tool_use.main,
         {"session_id": "s", "cwd": cwd, "agent_id": "def", "hook_event_name": "PostToolUse", "tool_name": "Bash",
          "tool_input": {"command": "ls"}, "tool_response": {"stdout": "a"}, "tool_use_id": "lost"}, monkeypatch)
    monkeypatch.setattr(Ledger, "append_many", real)
    assert "lost" not in (_common.load_state(cwd, "s").get("seen_tuid") or [])
    tr = hook_env["tmp"] / "agent-def.jsonl"
    _sub_transcript(tr, "lost")
    _run("subagent_stop", subagent_stop.main,
         {"session_id": "s", "cwd": cwd, "agent_id": "def", "hook_event_name": "SubagentStop",
          "agent_transcript_path": str(tr)}, monkeypatch)
    assert len(_rows(hook_env["ledger"], "lost")) == 1


def test_c_subagent_stop_first_then_live_hook_writes_one_row(hook_env, monkeypatch):
    cwd = hook_env["cwd"]
    tr = hook_env["tmp"] / "agent-def.jsonl"
    _sub_transcript(tr, "st9")
    _run("subagent_stop", subagent_stop.main,
         {"session_id": "s", "cwd": cwd, "agent_id": "def", "hook_event_name": "SubagentStop",
          "agent_transcript_path": str(tr)}, monkeypatch)
    _run("post_tool_use", post_tool_use.main,
         {"session_id": "s", "cwd": cwd, "agent_id": "def", "hook_event_name": "PostToolUse", "tool_name": "Bash",
          "tool_input": {"command": "ls"}, "tool_response": {"stdout": "a"}, "tool_use_id": "st9"}, monkeypatch)
    assert len(_rows(hook_env["ledger"], "st9")) == 1


# ---- d. installer recognises the executed script, not a substring ------------------

def test_d_installer_matches_only_the_executed_adapter(tmp_path):
    inst = _load_installer()
    adapter = str(inst.HOOKS / "stop.py")
    py = sys.executable
    keel_cmd = f'"{py}" -S -E "{adapter}"'
    echo_cmd = f'echo "{adapter}"'
    backup_cmd = f'"{py}" "{adapter}.backup"'
    other_cmd = f'"{py}" other.py "{adapter}"'
    assert inst._is_keel_hook({"type": "command", "command": keel_cmd})
    for cmd in (echo_cmd, backup_cmd, other_cmd):
        assert not inst._is_keel_hook({"type": "command", "command": cmd}), cmd
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    hooks = [{"type": "command", "command": c} for c in (keel_cmd, echo_cmd, backup_cmd)]
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": hooks}]}}), encoding="utf-8")
    inst.install(settings, remove=True)
    left = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"]
    assert [h["command"] for h in left] == [echo_cmd, backup_cmd]


# ---- e. close and verify --------------------------------------------------------------

def test_e_close_and_a_concurrent_failed_verify_agree(tmp_path, monkeypatch):
    led = Ledger(tmp_path / "l.jsonl")
    _open_verify(led, tmp_path)
    opened = contract.find_open(led, "t")
    late = schema.make_event("verify", session="s", project="p", src="cli", task="t",
                             data={"predicate": "manual:a", "ok": False, "evidence": "late failure",
                                   "dod_hash": opened["data"]["dod_hash"], "open_id": opened["data"]["open_id"],
                                   "root": opened["data"]["root"]})
    real_state = contract._state
    threads: list = []

    def racing_state(ledger, task):
        st = real_state(ledger, task)
        if not threads:  # a verify(False) lands between the check and the close write
            th = threading.Thread(target=lambda: Ledger(ledger.path).append(late))
            threads.append(th)
            th.start()
            th.join(0.5)
        return st

    monkeypatch.setattr(contract, "_state", racing_state)
    try:
        contract.close_task(led, task="t", outcome="verified", session="s", project="p")
        said_verified = True
    except contract.ContractError:
        said_verified = False
    threads[0].join(30)
    closes, _ = contract.replay(led.iter())
    if said_verified:
        assert closes and closes[-1][2] is True, "close returned verified but the replay does not prove it"
        assert vitals.compute(list(led.iter()))["vitals"]["V2_verified_done"]["value"] == 1.0


def test_e_stale_close_does_not_drop_the_current_open(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    contract.open_task(led, session="s", project="p", task="t", goal="g", dod=["manual:a"], root=tmp_path)
    old = contract.find_open(led, "t")
    contract.close_task(led, task="t", outcome="claimed", session="s", project="p")
    contract.open_task(led, session="s", project="p", task="t", goal="g", dod=["manual:a"], root=tmp_path)
    cur = contract.find_open(led, "t")
    led.append(schema.make_event("task.close", session="s", project="p", src="cli", task="t",
                                 data={"outcome": "abandoned", "evidence": [], "dod_hash": old["data"]["dod_hash"],
                                       "open_id": old["data"]["open_id"], "root": old["data"]["root"]}))
    now = contract.find_open(led, "t")
    assert now is not None and now["data"]["open_id"] == cur["data"]["open_id"]


# ---- f. interrupts ---------------------------------------------------------------------

def test_f_interrupts_are_not_loops_or_error_bursts():
    evs = [_call(f"i{n}", False, interrupted=True, failure=True) for n in range(5)]
    assert incidents.detect_loops(evs) == []
    assert incidents.detect_error_bursts(evs) == []


# ---- g. NO DATA is printed, not a zero ---------------------------------------------

def test_g_no_data_is_never_printed_as_a_number():
    r = vitals.compute([_call("a", True)])
    lines = [ln for ln in scoreboard.render_panel(r).splitlines() if "[NO DATA]" in ln]
    assert lines
    for ln in lines:
        assert ": NO DATA [NO DATA]" in ln, ln
    for ln in scoreboard.render_report(r, title="t").splitlines():
        cells = [c.strip() for c in ln.split("|")]
        if len(cells) > 6 and cells[1].startswith("V") and cells[5].startswith("NO DATA"):
            assert cells[3] == "NO DATA", ln


# ---- h. V13 spec and band agree --------------------------------------------------------

def test_h_v13_spec_matches_the_band():
    spec = (ROOT / "docs" / "VITALS_SPEC.md").read_text(encoding="utf-8")
    sec = spec.split("**V13 Layer tax.**", 1)[1].split("\n\n", 1)[0]
    band = json.loads((ROOT / "keel" / "vitals_bands.json").read_text(encoding="utf-8"))["bands"]["V13_layer_tax_ms"]["warn_above"]
    assert f"{band} ms" in sec and "500 ms" not in sec


# ---- i. prompt word sets expire ------------------------------------------------------

def test_i_prompt_word_sets_older_than_14_days_are_deleted(hook_env, monkeypatch):
    cwd = hook_env["cwd"]
    hist = Path(_common.keel_dir(cwd)) / "state" / "prompts.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)
    old = {"t": time.time() - 15 * 86400, "h": "oldhash", "w": ["alpha", "beta"], "s": "s0"}
    hist.write_text(json.dumps(old) + "\n", encoding="utf-8")
    _run("user_prompt_submit", user_prompt_submit.main,
         {"session_id": "s", "cwd": cwd, "hook_event_name": "UserPromptSubmit",
          "prompt": "build the sales report for August and send it"}, monkeypatch)
    lines = hist.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and "oldhash" not in lines[0]


# ---- j. backfill detectors respect the agent --------------------------------------------

def test_j_blind_edit_detector_splits_by_agent():
    def ev(agent, tool, ob):
        return schema.make_event("tool.call", session="s", project="p", agent=agent,
                                 data={"tool": tool, "path": "/x/a.py", "ok": True, "ms": 1, "in_bytes": 1,
                                       "out_bytes": ob, "sig": tool})
    assert len(incidents.detect_blind_edits([ev("sub:A", "Read", 500), ev("sub:B", "Edit", 10)])) == 1
    assert incidents.detect_blind_edits([ev("sub:A", "Read", 500), ev("sub:A", "Edit", 10)]) == []


# ---- k. proven_predicates(opened=...) -----------------------------------------------------

def test_k_proven_predicates_honours_opened(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    _open_verify(led, tmp_path, dod=("manual:old",))
    old = contract.find_open(led, "t")
    contract.close_task(led, task="t", outcome="verified", session="s", project="p")
    _open_verify(led, tmp_path, dod=("manual:new",))
    assert contract.proven_predicates(led, task="t", opened=old) == {"manual:old": True}
    assert contract.proven_predicates(led, task="t") == {"manual:new": True}


# ---- l. state migration and the agent cap -------------------------------------------------

def test_l_reads_in_old_state_are_migrated(hook_env):
    cwd = hook_env["cwd"]
    p = "/work/a.py"
    Path(_common.state_path(cwd, "s")).write_text(json.dumps({"read_paths": [norm_path(p)], "recent": []}), encoding="utf-8")
    out = _record(cwd, "Edit", p, "e1")
    assert not out or "BLIND_EDIT" not in json.dumps(out)


def test_l_agent_cap_never_evicts_the_head(hook_env):
    cwd = hook_env["cwd"]
    p = "/work/a.py"
    _record(cwd, "Read", p, "r0")
    for i in range(post_tool_use.MAX_AGENTS + 5):
        _record(cwd, "Read", f"/work/o{i}.py", f"o{i}", agent=f"a{i}")
    out = _record(cwd, "Edit", p, "e1")
    assert not out or "BLIND_EDIT" not in json.dumps(out)


# ---- m. project directories do not collide ---------------------------------------------

def test_m_project_directories_do_not_collide(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEL_HOME", str(tmp_path / "home"))
    _common._DIRS.clear()
    a, b = tmp_path / "a_b", tmp_path / "a-b"
    a.mkdir()
    b.mkdir()
    try:
        assert _common.keel_dir(str(a)) != _common.keel_dir(str(b))
    finally:
        _common._DIRS.clear()


def test_m_an_existing_old_directory_is_still_used(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("KEEL_HOME", str(home))
    _common._DIRS.clear()
    proj = tmp_path / "p1"
    proj.mkdir()
    base = os.path.abspath(str(proj))
    old = home / _common.encode_project_dir(base)
    old.mkdir(parents=True)
    (old / "project.json").write_text(json.dumps({"cwd": base}), encoding="utf-8")
    try:
        assert Path(_common.keel_dir(str(proj))) == old
    finally:
        _common._DIRS.clear()


# ---- n. live FANOUT does not depend on a clock boundary -----------------------------------

def test_n_fanout_survives_a_window_boundary(hook_env, monkeypatch):
    cwd = hook_env["cwd"]
    base = 1_800_000_000 - (1_800_000_000 % 15)
    outs = []
    for i, dt in enumerate((14.0, 14.2, 14.4, 15.0, 15.2, 15.4)):  # three on each side of a 15 s boundary
        monkeypatch.setattr(post_tool_use.time, "time", lambda t=base + dt: t)
        outs.append(post_tool_use.record({"session_id": "s", "cwd": cwd, "tool_name": "Agent", "tool_input": {"prompt": f"p{i}"},
                                          "tool_use_id": f"ag{i}"}, ok=True, out_bytes=2))
    assert sum(1 for o in outs if o and "FANOUT" in json.dumps(o)) == 1


# ---- o. corrupted numbers do not crash ----------------------------------------------------

def test_o_vitals_survive_corrupted_incident_details():
    evs = [_call("a", True)]
    for cls, det in (("STALL", {"minutes": "oops"}), ("STALL", None), ("STALL", {"minutes": float("inf")}),
                     ("BLIND_EDIT", None), ("STALL", "text")):
        evs.append(schema.make_event("incident", session="s", project="p",
                                     data={"cls": cls, "severity": incidents.CLASSES[cls]["severity"], "detail": det}))
    r = vitals.compute(evs)
    assert r["vitals"]["V9_stall_minutes"]["n"] >= 0


def test_o_safe_int_handles_infinity_and_nan():
    assert schema.safe_int(float("inf")) == 0
    assert schema.safe_int(float("-inf"), 7) == 7
    assert schema.safe_int(float("nan")) == 0
    assert schema.safe_int("1e400") == 0
