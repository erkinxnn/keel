# -*- coding: utf-8 -*-
"""Red-team self test of both ends: dirty input is caught, clean input passes, an empty ledger
gives solid NO DATA, outcome verified is not written without verify, the ledger loses no events
under concurrent writes, the hash chain catches an edit, a contract cannot be reopened, a forged
verify with a foreign hash does not count, a verdict from cli does not reach V1, the flood metric
measures model-visible output (not hook payloads), and V4 uses active time.

Run: python -m keel selftest    (expected: SELFTEST: PASS n/n, exit code 0)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import contract, extract, incidents, schema, vitals
from .backfill import _PromptHistory, parse_session
from .ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]


def _ev(ev: str, ts: datetime, data: dict, agent: str = "head", task: str | None = None, src: str = "test") -> dict:
    return schema.make_event(ev, session="s1", project="p", agent=agent, src=src, task=task,
                             ts=ts.isoformat(timespec="seconds"), data=data)


def _call(ts: datetime, tool: str, ok: bool | None = True, out_bytes: int = 10, sig: str | None = None,
          path: str | None = None, ms: int | None = 100, turn: int = 1, msg: str | None = None) -> dict:
    return _ev("tool.call", ts, {"tool": tool, "ok": ok, "ms": ms, "in_bytes": 5, "out_bytes": out_bytes,
                                  "sig": sig or incidents.signature(tool, {"x": tool}), "path": path, "turn": turn, "msg": msg})


def _spawn_writers(path: Path, writers: int, per: int) -> list[subprocess.Popen]:
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from keel.ledger import Ledger\n"
        "from keel import schema\n"
        "led = Ledger(%r)\n"
        "for i in range(%d):\n"
        "    led.append(schema.make_event('tool.call', session='s', project='p', src='test', data={'tool': 'Bash', 'ok': True, 'ms': i, 'in_bytes': 1, 'out_bytes': 1, 'sig': 'x', 'path': None, 'turn': 1}))\n"
    ) % (str(ROOT), str(path), per)
    return [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(writers)]


def run() -> int:
    checks: list[tuple[str, bool, str]] = []
    t0 = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # --- LOOP: 3 identical with a failure = loop; 3 identical successful = no; 4 successful = loop
    add("LOOP 3 with failure dirty", len(incidents.detect_loops([_call(t0, "Bash", ok=False, sig="same") for _ in range(3)])) == 1)
    add("LOOP 3 clean repeats is not loop", len(incidents.detect_loops([_call(t0, "Bash", sig="same") for _ in range(3)])) == 0)
    add("LOOP 4 clean repeats dirty", len(incidents.detect_loops([_call(t0, "Bash", sig="same") for _ in range(4)])) == 1)
    add("LOOP different sigs clean", len(incidents.detect_loops([_call(t0, "Bash", ok=False, sig=f"s{i}") for i in range(4)])) == 0)
    add("signature ignores shell whitespace", incidents.signature("Bash", {"command": "pytest  -q"}) == incidents.signature("Bash", {"command": "pytest -q"}))

    # --- BLIND_EDIT
    add("BLIND_EDIT dirty", len(incidents.detect_blind_edits([_call(t0, "Edit", path="a.py")])) == 1)
    add("BLIND_EDIT clean after real read", len(incidents.detect_blind_edits([_call(t0, "Read", path="A.PY", out_bytes=500), _call(t0, "Edit", path="a.py")])) == 0)
    add("BLIND_EDIT tiny read does not count", len(incidents.detect_blind_edits([_call(t0, "Read", path="a.py", out_bytes=40), _call(t0, "Edit", path="a.py")])) == 1)
    add("BLIND_EDIT write-new clean", len(incidents.detect_blind_edits([_call(t0, "Write", path="new.py"), _call(t0, "Edit", path="new.py")])) == 0)
    add("BLIND_EDIT auto-loaded excluded", len(incidents.detect_blind_edits([_call(t0, "Edit", path="C:/x/CLAUDE.md")])) == 0)
    add("BLIND_EDIT path normalization", len(incidents.detect_blind_edits([_call(t0, "Read", path="C:/x/./a.py", out_bytes=500), _call(t0, "Edit", path="C:\\x\\a.py")])) == 0)

    # --- FLOOD
    add("FLOOD dirty", len(incidents.detect_floods([_call(t0, "Read", out_bytes=25_000)])) == 1)
    add("FLOOD clean", len(incidents.detect_floods([_call(t0, "Read", out_bytes=19_999)])) == 0)

    # --- STALL: one incident per wait, parallel calls do not double it
    add("STALL dirty", len(incidents.detect_stalls([_call(t0, "Bash", ms=31 * 60_000)])) == 1)
    add("STALL clean", len(incidents.detect_stalls([_call(t0, "Bash", ms=29 * 60_000)])) == 0)
    add("STALL parallel calls counted once", len(incidents.detect_stalls([_call(t0, "Bash", ms=31 * 60_000), _call(t0, "Read", ms=31 * 60_000)])) == 1)

    # --- ERROR BURST
    add("BURST dirty", len(incidents.detect_error_bursts([_call(t0, "Bash", ok=False) for _ in range(3)])) == 1)
    add("BURST clean", len(incidents.detect_error_bursts([_call(t0, "Bash", ok=False), _call(t0, "Bash", ok=True), _call(t0, "Bash", ok=False)])) == 0)

    # --- FANOUT per model message
    add("FANOUT dirty same msg", len(incidents.detect_fanout([_call(t0, "Agent", msg="m1") for _ in range(4)])) == 1)
    add("FANOUT clean different msgs", len(incidents.detect_fanout([_call(t0, "Agent", msg=f"m{i}") for i in range(4)])) == 0)
    add("FANOUT clean three", len(incidents.detect_fanout([_call(t0, "Agent", msg="m1") for _ in range(3)])) == 0)

    # --- UNVERIFIED_CLAIM
    c_dirty = _ev("claim", t0, {"kind": "done", "words": ["done"], "evidence_n": 0, "tool_calls_in_turn": 0})
    c_clean = _ev("claim", t0, {"kind": "done", "words": ["done"], "evidence_n": 2, "tool_calls_in_turn": 3})
    add("UNVERIFIED_CLAIM dirty", len(incidents.detect_unverified_claims([c_dirty])) == 1)
    add("UNVERIFIED_CLAIM clean", len(incidents.detect_unverified_claims([c_clean])) == 0)

    # --- claim_scan: negation window and questions
    add("claim positive", incidents.claim_words("All done, tests PASS.") != [])
    add("claim negation window", incidents.claim_words("Not done yet, the tests have not passed yet.") == [])
    add("claim negation far away does not cancel", incidents.claim_words("Done, not counting the details.") == ["done"])
    add("claim question excluded", incidents.claim_words("Done?") == [])
    add("claim negated counted", incidents.claim_scan("Not done.")[1] == 1)
    add("claim typographic apostrophe negates", incidents.claim_words("It isn\u2019t done.") == [])

    # --- REPEAT_ORDER + MEMORY_LOST + MEASURING
    add("REPEAT_ORDER dirty", len(incidents.detect_repeat_orders([_ev("prompt", t0, {"chars": 10, "words": 8, "repeat_of": "abc"})])) == 1)
    add("REPEAT_ORDER clean", len(incidents.detect_repeat_orders([_ev("prompt", t0, {"chars": 10, "words": 8, "repeat_of": None})])) == 0)
    add("MEMORY_LOST dirty", len(incidents.detect_memory_lost([_ev("session.start", t0, {"memory_attached": False})])) == 1)
    add("MEMORY_LOST clean", len(incidents.detect_memory_lost([_ev("session.start", t0, {"memory_attached": True})])) == 0)
    add("MEASURING dirty", len(incidents.detect_measuring_cost([_ev("layer.cost", t0, {"hook": "x", "ms": 900, "injected_bytes": 0}) for _ in range(6)])) == 1)
    add("MEASURING clean", len(incidents.detect_measuring_cost([_ev("layer.cost", t0, {"hook": "x", "ms": 30, "injected_bytes": 0}) for _ in range(6)])) == 0)

    # --- repeat by history
    dirty = [_call(t0 + timedelta(seconds=i), "Bash", ok=False, sig="same") for i in range(3)]
    incs = incidents.detect_loops(dirty)
    incidents.mark_repeats(incs, history_classes=set())
    add("repeat first time false", incs[0]["data"]["repeat"] is False)
    incs2 = incidents.detect_loops(dirty)
    incidents.mark_repeats(incs2, history_classes={"LOOP"})
    add("repeat second time true", incs2[0]["data"]["repeat"] is True)

    # --- vitals: an empty ledger = solid NO DATA (negative control)
    empty = vitals.compute([])
    add("vitals empty -> all NO DATA", all(v["status"] == "NO DATA" for v in empty["vitals"].values()),
        json.dumps({k: v["status"] for k, v in empty["vitals"].items()}, ensure_ascii=False))
    some = vitals.compute([_call(t0, "Bash", ok=True), _call(t0, "Bash", ok=True)])
    add("vitals zero with n is not NO DATA", some["vitals"]["V6_tool_error_rate"]["status"] != "NO DATA" and some["vitals"]["V6_tool_error_rate"]["value"] == 0.0)
    # calibration: fewer than 8 points = NO DATA; coin flip = NO DATA; an honest forecast is computed
    def closes(p: float, n: int, verified: bool = True):
        return [_ev("task.close", t0, {"outcome": "verified" if verified else "claimed", "forecast_p": p, "dod": ["file_exists:x"], "dod_hash": "h"}, task=f"t{i}") for i in range(n)]
    add("brier n<8 -> NO DATA", vitals.compute(closes(0.9, 7))["vitals"]["V14_calibration_brier"]["status"] == "NO DATA")
    add("brier coin -> NO DATA", vitals.compute(closes(0.5, 8))["vitals"]["V14_calibration_brier"]["status"] == "NO DATA")
    honest = closes(0.9, 8, verified=False)  # claimed without verify: outcome 0 -> Brier 0.81 > 0.25 -> WARN, but not NO DATA
    add("brier honest forecast -> value", vitals.compute(honest)["vitals"]["V14_calibration_brier"]["value"] is not None)
    add("V14 extra mean_p", vitals.compute(honest)["vitals"]["V14_calibration_brier"]["extra"]["mean_p"] == 0.9)

    # --- V2: verified without verify events carrying the same hash does not count
    close_ok = _ev("task.close", t0, {"outcome": "verified", "dod": ["file_exists:x"], "dod_hash": "h1"}, task="T")
    ver_ok = _ev("verify", t0, {"predicate": "file_exists:x", "ok": True, "evidence": "e", "dod_hash": "h1"}, task="T")
    ver_bad = _ev("verify", t0, {"predicate": "file_exists:x", "ok": True, "evidence": "e", "dod_hash": "OTHER"}, task="T")
    add("V2 verified with matching verify", vitals.compute([close_ok, ver_ok])["vitals"]["V2_verified_done"]["value"] == 1.0)
    add("V2 verified without verify not counted", vitals.compute([close_ok])["vitals"]["V2_verified_done"]["extra"]["unproven_verified"] == 1
        and vitals.compute([close_ok])["vitals"]["V2_verified_done"]["value"] == 0.0)
    add("V2 verify with other hash not counted", vitals.compute([close_ok, ver_bad])["vitals"]["V2_verified_done"]["value"] == 0.0)

    # --- V1: a cli verdict does not count, tg does
    v_cli = _ev("owner.verdict", t0, {"artifact": "a", "verdict": "accept", "reasons": []}, src="cli")
    v_tg = _ev("owner.verdict", t0, {"artifact": "a", "verdict": "accept", "reasons": []}, src="tg")
    add("V1 cli accept -> NO DATA", vitals.compute([v_cli])["vitals"]["V1_accepted"]["status"] == "NO DATA")
    add("V1 tg accept counts", vitals.compute([v_tg])["vitals"]["V1_accepted"]["value"] == 1.0)

    # --- V9: union of intervals
    st1 = _ev("incident", t0, {"cls": "STALL", "severity": "high", "repeat": False, "detail": {"minutes": 60.0}})
    st2 = _ev("incident", t0, {"cls": "STALL", "severity": "high", "repeat": False, "detail": {"minutes": 60.0}})
    r9 = vitals.compute([_call(t0, "Bash"), st1, st2])
    add("V9 overlapping stalls not summed", r9["vitals"]["V9_stall_minutes"]["value"] == 60.0 and r9["vitals"]["V9_stall_minutes"]["n"] == 1)

    # --- V8: by distinct files
    be = [_ev("incident", t0, {"cls": "BLIND_EDIT", "severity": "medium", "repeat": False, "detail": {"path": "a.py"}})]
    r8 = vitals.compute([_call(t0, "Edit", path="a.py"), _call(t0, "Edit", path="a.py"), _call(t0, "Edit", path="b.py")] + be)
    add("V8 by distinct files", abs(r8["vitals"]["V8_blind_edit_rate"]["value"] - 0.5) < 1e-9 and r8["vitals"]["V8_blind_edit_rate"]["n"] == 2)

    # --- corrupted types: the schema refuses, and junk already on disk does not crash the board
    try:
        schema.make_event("usage", session="s", project="p", data={"input": "a lot"})
        add("schema rejects bad int", False)
    except schema.SchemaError:
        add("schema rejects bad int", True)
    junk = {"ts": t0.isoformat(), "ev": "usage", "session": "s", "project": "p", "agent": "head", "src": "test",
            "data": {"input": "a lot", "output": 5}}
    r_j = vitals.compute([junk, _call(t0, "Bash")])
    add("vitals survive junk usage", r_j["vitals"]["V4_tokens_per_hour"]["status"] == "NO DATA" or r_j["events"] == 2)

    # --- bands: every band has a source; a reference band never gives WARN; V14 inclusive
    add("bands have source", all(b.get("source") for b in vitals.BANDS.values()))
    ref_only = vitals.compute([_call(t0, "Bash", ok=False), _call(t0, "Bash", ok=False)])
    add("reference band never WARN", ref_only["vitals"]["V6_tool_error_rate"]["status"] == "REF")

    # --- balance
    add("balance empty -> NO DATA", empty["balance"]["value"] is None)
    bal_ev = [v_tg, close_ok, ver_ok,
              _ev("incident", t0, {"cls": "STALL", "severity": "high", "repeat": True, "detail": {"minutes": 40.0}}),
              _ev("incident", t0, {"cls": "BLIND_EDIT", "severity": "medium", "repeat": False, "detail": {"path": "x"}}),
              _ev("incident", t0, {"cls": "LOOP", "severity": "medium", "repeat": True, "detail": {}})]
    add("balance formula 3+1-2-1 = 1", vitals.compute(bal_ev)["balance"]["value"] == 1, str(vitals.compute(bal_ev)["balance"]))

    with tempfile.TemporaryDirectory() as td:
        led = Ledger(Path(td) / "l.jsonl")
        f = Path(td) / "out.txt"
        # --- contract: verified without verify is refused; after verify it passes; reopening is refused
        contract.open_task(led, session="s", project="p", task="T1", goal="make a file", dod=[f"file_min_bytes:{f}:5"], forecast_p=0.8)
        try:
            contract.open_task(led, session="s", project="p", task="T1", goal="swap", dod=["cmd_exit0:echo ok"])
            add("reopen refused", False)
        except contract.ContractError:
            add("reopen refused", True)
        try:
            contract.close_task(led, task="T1", outcome="verified", session="s", project="p")
            add("verified without verify refused", False)
        except contract.ContractError:
            add("verified without verify refused", True)
        # a forged verify with a foreign hash written straight into the ledger
        led.append(schema.make_event("verify", session="s", project="p", src="test", task="T1",
                                     data={"predicate": f"file_min_bytes:{f}:5", "ok": True, "evidence": "fake", "dod_hash": "deadbeef"}))
        try:
            contract.close_task(led, task="T1", outcome="verified", session="s", project="p")
            add("forged verify with other hash refused", False)
        except contract.ContractError:
            add("forged verify with other hash refused", True)
        # an appended "reopen" with an easy predicate does not replace the contract
        led.append(schema.make_event("task.open", session="s", project="p", src="test", task="T1",
                                     data={"goal": "swap", "dod": ["cmd_exit0:echo ok"], "dod_hash": contract.dod_hash(["cmd_exit0:echo ok"])}))
        add("appended reopen ignored by find_open", contract.find_open(led, "T1")["data"]["dod"] == [f"file_min_bytes:{f}:5"])
        f.write_text("hello world", encoding="utf-8")
        res = contract.verify(led, task="T1", cwd=Path(td), session="s", project="p")
        add("verify ok", all(e["data"]["ok"] for e in res))
        e = contract.close_task(led, task="T1", outcome="verified", session="s", project="p")
        add("close verified after verify", e["data"]["outcome"] == "verified" and e["data"]["dod_hash"] == contract.dod_hash([f"file_min_bytes:{f}:5"]))
        try:
            contract.close_task(led, task="T1", outcome="claimed", session="s", project="p")
            add("double close refused", False)
        except contract.ContractError:
            add("double close refused", True)
        # manual predicate: closed only by --ok with evidence
        contract.open_task(led, session="s", project="p", task="T2", goal="manual", dod=["manual:owner listened"])
        try:
            contract.verify(led, task="T2", cwd=Path(td), session="s", project="p", manual_ok={"manual:owner listened": "  "})
            add("manual without evidence refused", False)
        except contract.ContractError:
            add("manual without evidence refused", True)
        r2 = contract.verify(led, task="T2", cwd=Path(td), session="s", project="p", manual_ok={"manual:owner listened": "owner said it sounds right"})
        add("manual with evidence ok", all(x["data"]["ok"] for x in r2))
        # verdicts
        try:
            contract.record_verdict(led, session="s", project="p", artifact="a.mp4", verdict="reject", reasons=[])
            add("reject without reasons refused", False)
        except contract.ContractError:
            add("reject without reasons refused", True)
        contract.record_verdict(led, session="s", project="p", artifact="a.mp4", verdict="accept", reasons=[], src="cli")
        r = vitals.compute(led.all())
        add("V1 not inflated by cli verdict", r["vitals"]["V1_accepted"]["status"] == "NO DATA" and r["vitals"]["V1_accepted"]["extra"]["untrusted_accepts"] == 1)
        add("V2 counts real verified", r["vitals"]["V2_verified_done"]["value"] == 1.0)
        # hash chain: the audit is clean, an edited line is caught
        add("audit clean", led.audit()["ok"])
        lines = Path(led.path).read_text(encoding="utf-8").splitlines()
        lines[2] = lines[2].replace('"ok":true', '"ok":false') if '"ok":true' in lines[2] else lines[2].replace('"src":"test"', '"src":"tg"')
        Path(led.path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        add("audit catches tamper", not led.audit()["ok"])
        # schema: a malformed event is not written
        try:
            led.append({"ts": "x", "ev": "nope"})
            add("schema rejects bad event", False)
        except schema.SchemaError:
            add("schema rejects bad event", True)

        # --- concurrent writes: 8 writers x 50 events, no loss, chain intact
        cpath = Path(td) / "conc.jsonl"
        procs = _spawn_writers(cpath, 8, 50)
        errs = [p.communicate(timeout=120)[1] for p in procs]
        clean = Ledger(cpath)
        n_lines = sum(1 for _ in clean.iter())
        add("concurrent append loses nothing", n_lines == 400 and clean.bad_lines == 0, f"lines={n_lines} bad={clean.bad_lines} rc={[p.returncode for p in procs]} err={[e[-120:] for e in errs if e][:2]}")
        add("concurrent append chain intact", clean.audit()["ok"])

        # --- concurrent session state: 8 processes, no lost write
        from keel.hooks._common import locked_state  # noqa: PLC0415
        code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r)\n"
                "from _common import locked_state\n"
                "import os\n"
                "with locked_state(%r, 'race') as st:\n"
                "    st.setdefault('pending', {})[str(os.getpid())] = 1\n") % (str(ROOT), str(ROOT / "keel" / "hooks"), td)
        sp = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(8)]
        serrs = [p.communicate(timeout=120)[1] for p in sp]
        with locked_state(td, "race") as st:
            got = len(st.get("pending", {}))
        add("concurrent state keeps all writers", got == 8, f"got={got} err={[e[-100:] for e in serrs if e][:2]}")

        # --- backfill on a synthetic transcript
        tr = Path(td) / "sess.jsonl"
        base = {"sessionId": "S", "cwd": "C:\\\\Users\\\\x\\\\proj", "isSidechain": False}
        lines = [
            {**base, "type": "user", "timestamp": "2026-09-02T10:00:00Z", "message": {"role": "user", "content": "build the sales report for August please do it fast"}},
            {**base, "type": "assistant", "timestamp": "2026-09-02T10:00:05Z", "requestId": "r1",
             "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "text", "text": "Done, the report is built."}]}},
            {**base, "type": "assistant", "timestamp": "2026-09-02T10:00:06Z", "requestId": "r1",
             "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "text", "text": "(a duplicate line with the same requestId)"}]}},
            {**base, "type": "user", "timestamp": "2026-09-02T10:00:30Z", "message": {"role": "user", "content": "This session is being continued from a previous conversation. Summary of the conversation: ..."}},
            {**base, "type": "user", "timestamp": "2026-09-02T10:01:00Z", "message": {"role": "user", "content": "build the sales report for August please do it fast"}},
            {**base, "type": "assistant", "timestamp": "2026-09-02T10:01:05Z", "requestId": "r2",
             "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "echo hi"}},
                                     {"type": "tool_use", "id": "a1", "name": "Agent", "input": {"prompt": "1"}},
                                     {"type": "tool_use", "id": "a2", "name": "Agent", "input": {"prompt": "2"}},
                                     {"type": "tool_use", "id": "a3", "name": "Agent", "input": {"prompt": "3"}},
                                     {"type": "tool_use", "id": "a4", "name": "Agent", "input": {"prompt": "4"}}]}},
            {**base, "type": "user", "timestamp": "2026-09-02T10:01:07Z", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "hi", "is_error": False}]}},
            {**base, "type": "user", "timestamp": "2026-09-02T10:01:08Z", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a1", "content": "ok"}, {"type": "tool_result", "tool_use_id": "a2", "content": "ok"}, {"type": "tool_result", "tool_use_id": "a3", "content": "ok"}, {"type": "tool_result", "tool_use_id": "a4", "content": "ok"}]}},
            {**base, "type": "assistant", "timestamp": "2026-09-02T10:01:09Z", "requestId": "r3",
             "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 10, "output_tokens": 5},
                         "content": [{"type": "text", "text": "Finished, output received."}]}},
        ]
        tr.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n", encoding="utf-8")
        ev = parse_session(tr, project="p", memory_attached=True, prompt_history=_PromptHistory())
        kinds = [e["ev"] for e in ev]
        claims = [e for e in ev if e["ev"] == "claim"]
        prompts = [e for e in ev if e["ev"] == "prompt"]
        add("backfill usage dedup by requestId", kinds.count("usage") == 3, f"usage={kinds.count('usage')}")
        add("backfill usage keeps the request id", sorted(e["data"].get("req") for e in ev if e["ev"] == "usage") == ["r1", "r2", "r3"])
        add("backfill harness summary not a prompt", len(prompts) == 2, f"prompts={len(prompts)}")
        add("backfill two claims", len(claims) == 2, f"claims={len(claims)}")
        add("backfill first claim no evidence", claims[0]["data"]["evidence_n"] == 0 and claims[0]["data"]["tool_calls_in_turn"] == 0)
        add("backfill second claim has evidence", claims[1]["data"]["evidence_n"] >= 1)
        add("backfill repeat prompt detected", prompts[1]["data"]["repeat_of"] is not None)
        found = incidents.detect_all(ev)
        cls = {i["data"]["cls"] for i in found}
        add("backfill incidents UNVERIFIED_CLAIM+REPEAT_ORDER+FANOUT", {"UNVERIFIED_CLAIM", "REPEAT_ORDER", "FANOUT"} <= cls, str(cls))
        add("backfill no owner text stored", not any("sales" in json.dumps(e, ensure_ascii=False) for e in ev))

    # ---- cheap primitives and an honest V13 (both ends) --------------------------
    import hashlib as _hl
    import statistics as _st
    from ._fast import median as _med, sha256_hex as _sha
    from .hooks import _common as _c  # noqa: PLC0415
    # the corpus deliberately includes a non-ASCII string (Latin letter with an accent)
    corpus = [b"", b"a", "Caf\u00e9".encode("utf-8"), os.urandom(64), b"x" * 5000]
    add("fast sha256 == hashlib", all(_sha(b) == _hl.sha256(b).hexdigest() for b in corpus))
    samples = [[1], [1, 2], [3, 1, 2], [1.5, 2.5, 3.5, 4.5], list(range(9))]
    add("fast median == statistics", all(abs(_med(x) - _st.median(x)) < 1e-9 for x in samples))
    try:
        _med([])
        add("fast median refuses empty", False)
    except ValueError:
        add("fast median refuses empty", True)
    import re as _re
    # deliberately non-ASCII inputs (accented Latin, math symbols): every non-[A-Za-z0-9] char becomes a dash
    dirs = [str(ROOT), "C:/Users/dev/Desktop/proj", "/home/x/caf\u00e9-1", "a.b_c~d", "", "\u03a9\u2248\u00e7\u221a"]
    add("project dir encoding == old regex",
        all(_c.encode_project_dir(d) == _re.sub(r"[^A-Za-z0-9]", "-", d) for d in dirs))
    iso = schema.now_iso()
    add("now_iso has zone and seconds", len(iso) == 25 and iso[10] == "T" and iso[-3] == ":" and iso[-6] in "+-", iso)
    add("now_iso parses back", abs((schema.parse_ts(iso) - datetime.now(timezone.utc)).total_seconds()) < 120)
    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "tax.jsonl"
        led = Ledger(lp)
        now = datetime.now(timezone.utc)
        for i in range(6):
            led.append(_ev("layer.cost", now, {"hook": "post_tool_use", "ms": 40, "injected_bytes": 0}))
        r = vitals.compute(led.all())
        v13 = r["vitals"]["V13_layer_tax_ms"]
        add("V13 internal only without wall", v13["value"] == 40 and v13["extra"]["wall_ms"] is None)
        add("V13 no false MEASURING_BECAME_COST", not incidents.detect_measuring_cost(led.all()))
        led.append(_ev("layer.cost", now, {"hook": "post_tool_use", "ms": 9000, "injected_bytes": 0,
                                           "kind": "wall", "freq": "call", "floor_ms": 76}))
        r2 = vitals.compute(led.all())
        v13b = r2["vitals"]["V13_layer_tax_ms"]
        add("V13 wall event not counted as internal", v13b["value"] == 40, str(v13b["value"]))
        add("V13 prints wall next to internal", v13b["extra"]["wall_ms"] == 9000 and "9000" in v13b["hint"])
        add("wall event does not trigger MEASURING_BECAME_COST", not incidents.detect_measuring_cost(led.all()))
        for i in range(10):
            led.append(_ev("layer.cost", now, {"hook": "post_tool_use", "ms": 900, "injected_bytes": 0}))
        add("MEASURING_BECAME_COST fires on slow internal", bool(incidents.detect_measuring_cost(led.all())))
    # two thresholds: own work 50 ms, full call 250 ms; both ends
    with tempfile.TemporaryDirectory() as td:
        led = Ledger(Path(td) / "tax2.jsonl")
        now = datetime.now(timezone.utc)
        for i in range(6):
            led.append(_ev("layer.cost", now, {"hook": "post_tool_use", "ms": 40, "injected_bytes": 0}))
        led.append(_ev("layer.cost", now, {"hook": "post_tool_use", "ms": 170, "med_ms": 195, "injected_bytes": 0,
                                           "kind": "wall", "freq": "call", "floor_ms": 76}))
        v = vitals.compute(led.all())["vitals"]["V13_layer_tax_ms"]
        add("V13 own work under band is clean", v["status"] != "WARN" and "threshold" not in v["hint"], v["hint"])
        led2 = Ledger(Path(td) / "tax3.jsonl")
        for i in range(6):
            led2.append(_ev("layer.cost", now, {"hook": "post_tool_use", "ms": 80, "injected_bytes": 0}))
        led2.append(_ev("layer.cost", now, {"hook": "post_tool_use", "ms": 300, "med_ms": 400, "injected_bytes": 0,
                                            "kind": "wall", "freq": "call", "floor_ms": 76}))
        v2 = vitals.compute(led2.all())["vitals"]["V13_layer_tax_ms"]
        add("V13 own work over band flagged", v2["status"] == "REF" and v2["value"] == 80, f"{v2['status']} {v2['value']}")
        add("V13 full call over band flagged", "full call 400 ms is beyond the reference threshold 250" in v2["hint"], v2["hint"])

    # ---- fix a: the flood metric measures what ENTERS THE MODEL CONTEXT ------------
    # Edit/Write payloads carry originalFile/oldString/newString/structuredPatch that the model never sees.
    big_edit = {"filePath": "a.py", "originalFile": "x" * 40_000, "oldString": "a" * 5_000, "newString": "b" * 5_000,
                "structuredPatch": [{"lines": ["+" + "c" * 4_000]}], "userModified": False, "replaceAll": False}
    big_write = {"type": "create", "filePath": "n.py", "content": "y" * 30_000, "structuredPatch": [], "originalFile": None}
    add("flood: the old ruler would have counted the Edit payload as a flood", extract.content_len(big_edit) > incidents.FLOOD_CHARS)
    add("flood: an Edit payload counts as a short confirmation", extract.tool_result_len("Edit", big_edit) == extract.EDIT_CONFIRM_CHARS)
    add("flood: a Write payload counts as a short confirmation", extract.tool_result_len("Write", big_write) == extract.EDIT_CONFIRM_CHARS)
    add("flood: a big Edit payload is not a FLOOD incident",
        not incidents.detect_floods([_call(t0, "Edit", path="a.py", out_bytes=extract.tool_result_len("Edit", big_edit))]))
    add("flood: an Edit error string is kept as written", extract.tool_result_len("Edit", "Error: string not found") == len("Error: string not found"))
    image = {"type": "image", "file": {"base64": "A" * 900_000, "type": "image/png", "originalSize": 700_000}}
    add("flood: an image counts as a fixed estimate (1600 tokens x 4 chars)",
        extract.tool_result_len("Read", image) == extract.IMAGE_CHARS == 6400)
    add("flood: one image is not a FLOOD incident", not incidents.detect_floods([_call(t0, "Read", out_bytes=extract.tool_result_len("Read", image))]))
    block_row = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 900_000}}, {"type": "text", "text": "abc"}]
    add("flood: image blocks in a transcript row", extract.content_len(block_row) == extract.IMAGE_CHARS + 3)
    text_read = {"type": "text", "file": {"filePath": "a.py", "content": "z" * 25_000, "numLines": 800, "startLine": 1, "totalLines": 800}}
    add("flood: a text Read keeps its text length", extract.tool_result_len("Read", text_read) == 25_000)
    add("flood: a big text Read is still a FLOOD incident",
        len(incidents.detect_floods([_call(t0, "Read", out_bytes=extract.tool_result_len("Read", text_read))])) == 1)
    add("flood: Bash stdout and stderr are counted",
        extract.tool_result_len("Bash", {"stdout": "o" * 30, "stderr": "e" * 12, "interrupted": False, "isImage": False}) == 42)
    v5 = vitals.compute([_call(t0, "Edit", path="a.py", out_bytes=extract.tool_result_len("Edit", big_edit)),
                         _call(t0, "Read", path="a.py", out_bytes=300)])["vitals"]["V5_flood_share"]
    add("flood: V5 stays at zero on edit-only traffic", v5["value"] == 0.0 and v5["extra"]["floods"] == 0, str(v5["value"]))

    # ---- fix b: V4 divides by ACTIVE time, not by the sum of session durations ------
    tb = datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc)
    burst1 = [tb + timedelta(seconds=s) for s in (0, 120, 300)]                    # 5 min of work
    burst2 = [tb + timedelta(hours=2, seconds=s) for s in (0, 120, 300)]           # 2 h later, 5 min of work
    ev_b = [_ev("session.start", burst1[0], {"cwd": "x", "memory_attached": True})]
    ev_b += [_call(t, "Bash") for t in burst1 + burst2]
    ev_b += [_ev("usage", burst1[1], {"input": 0, "output": 0, "cache_read": 600_000, "cache_create": 0}),
             _ev("usage", burst2[1], {"input": 0, "output": 0, "cache_read": 400_000, "cache_create": 0}),
             _ev("session.stop", burst2[2], {"reason": "exit", "duration_s": 7500, "turns": 2})]
    rb = vitals.compute(ev_b)
    v4 = rb["vitals"]["V4_tokens_per_hour"]
    add("V4 active time counts two bursts, not the 2-hour idle gap", v4["extra"]["active_seconds"] == 600, str(v4["extra"]["active_seconds"]))
    add("V4 value uses active hours (1,000,000 tokens / (1/6 h) = 6000 k/h)", v4["value"] is not None and abs(v4["value"] - 6000.0) < 1e-6, str(v4["value"]))
    add("V4 keeps the old session wall clock only as information", v4["extra"]["session_wall_clock_hours"] == 2.1)
    add("active time: a gap of exactly 600 s counts, 601 s is idle",
        vitals.active_seconds([_call(tb, "Bash"), _call(tb + timedelta(seconds=600), "Bash")]) == 600.0
        and vitals.active_seconds([_call(tb, "Bash"), _call(tb + timedelta(seconds=601), "Bash")]) == 0.0)
    other = dict(_call(tb + timedelta(seconds=100), "Bash")); other["session"] = "s2"
    other2 = dict(_call(tb + timedelta(seconds=700), "Bash")); other2["session"] = "s2"
    add("active time: parallel sessions add up, each measured on its own",
        vitals.active_seconds([_call(tb, "Bash"), _call(tb + timedelta(seconds=500), "Bash"), other, other2]) == 1100.0)
    add("active time: benchmark records (kind=wall) are ignored",
        vitals.active_seconds([_call(tb, "Bash"), _ev("layer.cost", tb + timedelta(seconds=9000), {"hook": "x", "ms": 1, "injected_bytes": 0, "kind": "wall"})]) == 0.0)
    u_data = {"input": 10, "output": 5, "cache_read": 0, "cache_create": 0}
    u1 = _ev("usage", tb, {**u_data, "req": "req_1"})
    u1_again = _ev("usage", tb + timedelta(seconds=30), {**u_data, "req": "req_1"})   # a re-parsed transcript writes it again
    u2 = _ev("usage", tb + timedelta(seconds=60), {**u_data, "req": "req_2"})
    rd = vitals.compute([_ev("session.start", tb, {"memory_attached": True}), u1, u1_again, u2])["vitals"]["V4_tokens_per_hour"]["extra"]
    add("usage with the same request id is counted once", rd["total"] == 30 and rd["usage_duplicates_dropped"] == 1 and rd["model_calls"] == 2, str(rd["total"]))
    n1 = _ev("usage", tb, u_data)
    n2 = _ev("usage", tb + timedelta(seconds=30), u_data)
    rn = vitals.compute([_ev("session.start", tb, {"memory_attached": True}), n1, n2])["vitals"]["V4_tokens_per_hour"]["extra"]
    add("usage without a request id cannot be de-duplicated and is all counted", rn["total"] == 30 and rn["usage_duplicates_dropped"] == 0)

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        if not ok:
            print(f"FAIL {name} {detail}")
    print(f"SELFTEST: {'PASS' if passed == len(checks) else 'FAIL'} {passed}/{len(checks)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(run())
