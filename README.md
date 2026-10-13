# KEEL

**Your agent says "done". KEEL records what actually happened.**

KEEL is a ledger for [Claude Code](https://code.claude.com) sessions, built on hooks. It writes every
tool call, token count, claim, verification and owner verdict into an append-only, hash-chained
JSONL file, then computes the agent's *vital signs* from those events and nothing else. It flags known
failure patterns the moment they repeat, and it lets a task be recorded as `verified` only after its
done-predicates have actually run and passed.

Pure standard library, Python 3.11+, no dependencies. Status: release candidate 0.1.0.

```
KEEL SCOREBOARD: demo (whole ledger)
sessions 0, active hours 0.0, events 4
V1 Accepted by owner: NO DATA [NO DATA] (no owner.verdict at all: set up an owner verdict channel (tg/owner))
V2 Proven done: 100.0 % [REF] (n=1)
V3 Claims with evidence: NO DATA [NO DATA] (no claim events)
...
```

(Real output of `python -m keel panel`, trimmed. Where there is no data, KEEL prints `NO DATA`, never a zero.)

## What it does

- **Immutable ledger.** One JSON line per event. Each event stores the hash of the previous one, so
  `python -m keel audit` finds any edited, inserted or deleted line. Writes are atomic between
  processes (a file lock, one `write` per batch, fsync), because Claude Code runs hooks in parallel.
- **Metrics computed only from events.** 14 vital signs (token burn per active hour, context flood,
  tool errors, retry loops, blind edits, idle waiting, subagent fan-out, repeated owner orders, claims
  with evidence, proven-done share, forecast calibration, the layer's own latency, ...). Each has a
  source event, a formula, a number of observations `n`, and a band whose threshold has a written
  source. A band without a source cannot turn a number red.
- **Automatic incident classes.** `LOOP`, `BLIND_EDIT`, `FLOOD`, `STALL`, `TOOL_ERROR_BURST`, `FANOUT`,
  `UNVERIFIED_CLAIM`, `REPEAT_ORDER`, `MEMORY_LOST`, `MEASURING_BECAME_COST`. When one fires in a live
  session the agent gets a one-line note, marked as a repeat if the class happened before. Six more
  classes are in the registry (`python -m keel classes`) but have no automatic detector yet: two
  need a contract (`SCOPE_CREEP`, `BUDGET_OVERRUN`), four are recorded by hand with `python -m keel incident`.
- **Verified means verified.** A task is opened with done-predicates (`file_min_bytes:out.mp4:1000000`,
  `cmd_exit0:pytest -q`, `manual:...`). The predicates are hashed and cannot be swapped after opening.
  `close --outcome verified` is refused unless `verify` ran every predicate and each passed under that
  hash. A verdict written from the CLI is marked `src=cli` and does **not** count as "accepted by owner".
- **A "done" without evidence is visible.** The Stop hook records completion words ("done", "fixed",
  "verified", ...) with the number of tool calls and successful results in that turn. With
  `KEEL_STRICT=1` it blocks such a reply, at most once per turn. By default it only logs and counts.
- **History baseline.** `python -m keel backfill` builds the same kind of ledger from your existing
  Claude Code transcripts, so the metrics have a "where I was" before you install any hook.

## Install

Requirements: Python 3.11+. Nothing to `pip install`.

```bash
git clone <this repository> keel
cd keel
python -m keel selftest
```

Expected last line: `SELFTEST: PASS n/n` (118/118 in this release candidate).

Register the hooks for **one project** (recommended; a global registration affects every project):

```bash
python tools/install_hooks.py --project /path/to/your/project
python tools/install_hooks.py --check
```

This writes `/path/to/your/project/.claude/settings.json` (after saving a `settings.json.bak_keel_*`
backup) with six hooks: SessionStart, UserPromptSubmit, PostToolUse, SubagentStop, Stop, SessionEnd.
Other hooks in the file are left alone. To remove them:

```bash
python tools/install_hooks.py --project /path/to/your/project --remove
```

`--global` does the same for `~/.claude/settings.json`. The commands run as `python -S -E <script>`;
that is deliberate (it skips site-packages processing at interpreter start). The settings snippet to
copy by hand is `keel/hooks/settings.snippet.json`.

The ledger and per-session state live **outside** your repository, in
`~/.keel/<encoded launch directory>/` (override with `KEEL_HOME`, or `KEEL_LEDGER` for the ledger
file). Print your own scoreboard once a session has run:

```bash
python -m keel panel --ledger ~/.keel/<encoded launch directory>/ledger.jsonl --days 14
```

## Quick start (commands that were actually run)

A task contract in an empty ledger, from the repository root. The first `verify` fails because the
file does not exist yet, and `close` refuses to record `verified`:

```bash
python -m keel open --ledger .keel/demo.jsonl --task T1 --goal "write a report" --dod "file_min_bytes:.keel/report.txt:20" --p 0.7
python -m keel verify --ledger .keel/demo.jsonl --task T1
python -m keel close --ledger .keel/demo.jsonl --task T1 --outcome verified
```

```
FAIL file_min_bytes:.keel/report.txt:20  <- .keel\report.txt is missing
VERIFY: FAIL 0/1
REFUSED: outcome verified is not accepted: predicates not proven: file_min_bytes:.keel/report.txt:20. Close as claimed or run verify.
```

Now do the work, verify again, close, and check the chain:

```bash
echo "revenue report for August" > .keel/report.txt
python -m keel verify --ledger .keel/demo.jsonl --task T1
python -m keel close --ledger .keel/demo.jsonl --task T1 --outcome verified
python -m keel audit --ledger .keel/demo.jsonl
python -m keel panel --ledger .keel/demo.jsonl
```

```
OK   file_min_bytes:.keel/report.txt:20  <- .keel\report.txt bytes=26 (need >= 20)
VERIFY: PASS 1/1
AUDIT: OK events 4, breaks 0
```

A baseline from history. The `--projects-dir` default is `~/.claude/projects`; for this README the
command was run against a one-session synthetic transcript directory, so only the path differs:

```bash
python -m keel backfill --projects-dir ~/.claude/projects --out data/ledger
python -m keel report --ledger data/ledger
```

Record an owner verdict (only `tg`, `owner` and `owner-file` sources count toward "accepted"):

```bash
python -m keel verdict --ledger .keel/demo.jsonl --artifact out/tape.mp4 --verdict reject --reason "seams audible at 2:10" --src owner
```

Run everything: `python tests/run_checks.py` (selftest, pytest, hook end-to-end, contract-bypass,
state-race and ledger-concurrency probes).

## KEEL and ccusage

[ccusage](https://github.com/ryoppippi/ccusage) (MIT) reads local usage data of coding-agent CLIs
and reports **tokens and cost in USD** by day, week, month, session and Claude Code 5-hour billing
block, for Claude Code and many other agents. If your question is "what did this cost me", use
ccusage: it is more mature, covers far more tools, and prices tokens; KEEL does not price anything.

KEEL answers a different question: **"what did the agent do, and can I trust its report?"** It records
behaviour (calls, retries, edits without a read, claims and the evidence behind them, owner verdicts)
in a ledger that cannot be quietly edited, and derives behavioural metrics. Token burn is one of its
fourteen vitals, but only as a rate over *active* time, not as money. The two tools are complementary
and do not conflict.

## Honest limits

- **A hash chain proves there were no after-the-fact edits, not that the content is true.** Anything the
  agent can write, it can write falsely. The chain has no signature and does not stop someone deleting
  the whole file or rebuilding it from scratch; there is no external copy. KEEL makes cheap
  inflation harder (see `tests/adversarial/a2_contract_bypass.py`), not impossible.
- **The metric bands were calibrated on one user's history.** Most thresholds are marked `reference`
  and never turn a vital red. Treat the numbers as a starting vocabulary and recalibrate on your own ledger.
- **Tested on Windows only** (Windows 11, Python 3.11). The Linux/macOS code path (`fcntl` locking) exists
  but has not been exercised.
- **The hook schema may change.** Field names were checked against the Claude Code hooks reference on
  2026-10-13 and match, with the gaps listed below. Claude Code evolves its hooks quickly.
- **Failed tool calls are mostly invisible live.** PostToolUse fires only for calls that succeed; failures
  go to a separate `PostToolUseFailure` event that KEEL does not handle yet. Live `tool errors`, `LOOP`
  with failure and `TOOL_ERROR_BURST` therefore under-count. History built by `backfill` is not affected.
- **"Evidence" is a proxy.** The Stop hook treats any successful tool result in the same turn as evidence
  for a "done". Only the contract flow (`open` / `verify` / `close`) is a strict proof.
- **Completion words are English only** (`keel/incidents.py`, `DONE_WORDS_RE`). Extend it for other languages.
- **Cost of the layer.** Each hook is a fresh Python process: roughly 150-300 ms wall clock per call on the
  test machine, of which 20-40 ms is KEEL's own work. `python -m keel doctor` measures your interpreter
  start. If that is too much, keep only the per-turn hooks (the installer already leaves PreToolUse off).
- **Two metric corrections in this release** (details in `docs/ARCHITECTURE.md`, sections 6 and 7):
  the context-flood metric now counts what enters the model's context (edit payloads such as `originalFile`
  no longer count; an image counts as a fixed estimate of 1,600 tokens x 4 chars), and token burn per hour
  now divides by *active* time (gaps over 10 minutes are idle) and counts a model request once when the
  event carries its request id. Ledgers written by older versions keep the old numbers.
- Packaging metadata is in `pyproject.toml`, but the package has not been published to PyPI and
  `pip install` was not tested; run it from a clone.

## Layout

```
keel/            core: schema, ledger, incidents, vitals, contract, scoreboard, backfill, CLI
keel/hooks/      Claude Code adapters + settings.snippet.json
tools/           install_hooks.py (register / remove the adapters)
tests/           pytest, hook end-to-end probe, adversarial probes (a2, a8, a9)
docs/            ARCHITECTURE.md, VITALS_SPEC.md
```

## License

MIT, see `LICENSE`.
