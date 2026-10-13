# KEEL: local agent observability and verification records for Claude Code

KEEL turns Claude Code hooks and transcripts into a local audit trail: one hash-chained JSONL ledger per
launch directory (its folder name carries a hash of the absolute path, so two directories never share a
ledger) with tool calls, failed calls, token usage, completion claims, task checks and verdicts. It is a
small agent observability layer that runs on your machine: it reports retry loops, large tool outputs,
tool errors and "done" claims without evidence, and it records a task as `verified` only after the
task's explicit predicates ran and passed.

Python 3.11+, standard library only, MIT. Early release, tested on Windows only. Failure capture needs
the PostToolUseFailure adapter (see Coverage). The ledger has a hash chain but no external integrity
anchor unless you keep a checkpoint yourself (see Audit).

```
KEEL SCOREBOARD: demo (whole ledger)
sessions 0, active hours 0.0, events 5
V1 Claimed owner accepts: NO DATA [NO DATA] (verdicts exist only from the CLI (src=cli), they do not count)
V2 Proven done: 100.0 % [REF] (n=1)
V3 Claims with evidence: NO DATA [NO DATA] (no claim events)
...
```

(Real output of `python -m keel panel` on the quick-start ledger below, trimmed. Where a vital has no data,
the value column says `NO DATA` instead of a number.)

## What it does

- **Hash-chained ledger.** One JSON line per event; each event stores the hash of the previous one.
  `python -m keel audit` detects an edited line and a line inserted, deleted or moved inside the chain. It
  does not detect lines cut off the end of the file, or a file deleted or rebuilt from scratch, unless you
  pass a checkpoint you stored elsewhere (`--expect-count N --expect-last-hash H`). Writes are serialized
  between processes (a file lock, one `write` per batch, fsync), because Claude Code runs hooks in parallel.
- **Metrics computed only from events.** 14 vital signs (token burn per active hour, context flood, tool
  errors, retry loops, blind edits, idle waiting, subagent fan-out, repeated owner orders, claims with
  evidence, proven-done share, forecast calibration, the layer's own latency, ...). Each has a source event,
  a formula, a number of observations `n`, and a band whose threshold has a written source. Most bands are
  reference-only and never turn a number red.
- **Incident detectors.** Live in the hooks: `LOOP`, `BLIND_EDIT`, `FLOOD`, `STALL`, `TOOL_ERROR_BURST`,
  `FANOUT`, `UNVERIFIED_CLAIM`, `REPEAT_ORDER`, `MEMORY_LOST`. Each is written to the ledger, marked as a
  repeat if the class happened before in that project. Tool-call and prompt incidents reach the agent as a
  one-line note, `MEMORY_LOST` as a line in the session-start scoreboard; `UNVERIFIED_CLAIM` is only logged
  unless `KEEL_STRICT=1`. `MEASURING_BECAME_COST` has a detector but no live caller (backfill ledgers carry
  no `layer.cost` events), so in practice it does not fire. Six more classes are in the registry (`python -m keel classes`) without a detector: two need a
  contract (`SCOPE_CREEP`, `BUDGET_OVERRUN`), four are recorded by hand with `python -m keel incident`.
- **Task contracts.** A task is opened with done-predicates (`file_min_bytes:out.mp4:1000000`,
  `cmd_exit0:pytest -q`, `manual:...`). Each open gets a unique `open_id` and pins the absolute project root;
  the contract hash covers the predicates and the root. `verify` runs the predicates in that root. `close
  --outcome verified` is refused unless, replaying the ledger in order, the latest verify of every predicate
  of THIS open passed. The check and the close are one critical section under the ledger's append lock: a
  verify written by another KEEL process meanwhile lands after the close and does not count for it. (A
  writer that bypasses KEEL and appends to the file directly is not serialized; the ledger is not
  authenticated.) A late close of an earlier open of the same task does not end the current open. The
  proven-done metric (V2) uses the same replay.
- **"Done" without evidence is visible.** The Stop hook records completion words ("done", "fixed",
  "verified", ...) with the number of tool calls and successful results in that turn. A "done" with no
  successful tool result in the turn is an incident; with `KEEL_STRICT=1` the reply is blocked, at most
  once per turn. By default it only logs and counts.
- **History baseline.** `python -m keel backfill` builds the same kind of ledger from your existing Claude
  Code transcripts, so the metrics have a "before" without installing any hook.

## Coverage

Which Claude Code hook events KEEL listens to, and what lands in the ledger. Events not in this table are
not recorded.

| Claude Code event | Adapter | Installer registers it | Recorded |
|---|---|---|---|
| SessionStart | `session_start.py` | yes | `session.start` (cwd, memory attached), `MEMORY_LOST`; injects the scoreboard |
| UserPromptSubmit | `user_prompt_submit.py` | yes | `prompt` (length, word count, hash, repeat of); `REPEAT_ORDER` |
| PreToolUse | `pre_tool_use.py` | no (optional) | nothing in the ledger; the call start time, for duration |
| PostToolUse | `post_tool_use.py` | yes | `tool.call` (ok, duration, sizes, path, `tool_use_id`); loop, flood, stall, blind edit, error burst, fan-out |
| PostToolUseFailure | `post_tool_use_failure.py` | yes | `tool.call` with `ok=false` (`interrupted` for user interrupts), error length only |
| SubagentStop | `subagent_stop.py` | yes | subagent usage, and subagent tool calls whose `tool_use_id` was not already written by the live hooks (an id is marked only after its row is written; a live call arriving after the import is not written again; duplicates that still reach the ledger are counted once, a finished row preferred) |
| Stop | `stop.py` | yes | `usage` of the turn, `claim`; `UNVERIFIED_CLAIM`, optional strict block |
| SessionEnd | `session_end.py` | yes | `session.stop` |
| anything else (permission requests, notifications, compaction, ...) | none | no | not recorded; a call blocked or denied before it ran is not a tool call |

## What KEEL stores

- **Hook events** hold no prompt text, no reply text, no file content, no command output and no error
  text. They do hold lengths, hashes, tool names, **file paths** (`tool.call.path`, incident details), the
  **launch directory**, transcript paths, and the completion words found in a reply (`claim.words`).
- **Contract and verdict events** (from the CLI) hold what you give them, verbatim: the goal text, the
  predicates (paths, regexes, **shell commands**), verify evidence (for `cmd_exit0` the **last 300 chars of
  stdout**, for `regex_in_file` up to 80 chars of the **matched text**), close evidence, verdict reasons.
- **Session state** next to the ledger: `state/prompts.jsonl` keeps the **set of words of each prompt** for
  repeat-order detection; entries older than 14 days are deleted when the next prompt is recorded (with no
  new prompt they stay on disk until one comes). `hooks.log` keeps adapter tracebacks.

Location: `~/.keel/<slug>-<hash>/`, where `<slug>` is the launch directory with every non-alphanumeric
character replaced by `-` and `<hash>` is 10 hex chars of the SHA-256 of its absolute path (a folder made by
0.1.0/0.2.0 under the bare slug is still used if its `project.json` names the same directory). Override with
`KEEL_HOME`, or `KEEL_LEDGER` for the ledger file. It is outside your repository. Before sharing a ledger:

```bash
python -m keel export --ledger <ledger.jsonl> --out share.jsonl
```

The export masks goal, predicates, evidence, verdict reasons and artifacts, incident details, paths and cwd
as `redacted:<hash>`. It keeps the original hashes, so the copy no longer passes `audit`. Do not share
`state/` or `hooks.log`. `--no-redact` copies as is.

## Install

Requirements: Python 3.11+. Nothing to `pip install`. Clone this repository, then run from its root:

```bash
git clone https://github.com/erkinxnn/keel
cd keel
python -m keel selftest
```

Expected last line: `SELFTEST: PASS n/n` (155/155 at the time of writing).

Register the hooks for **one project** (recommended; a global registration affects every project):

```bash
python tools/install_hooks.py --project /path/to/your/project
python tools/install_hooks.py --check --project /path/to/your/project
```

This writes `/path/to/your/project/.claude/settings.json` (after saving a `settings.json.bak_keel_*` backup,
through a temporary file and a rename) with seven hooks: SessionStart, UserPromptSubmit, PostToolUse,
PostToolUseFailure, SubagentStop, Stop, SessionEnd. Each command is the absolute path of the Python that
ran the installer, with `-S -E` (no site-packages processing at start). Only entries whose command executes
one of this checkout's adapter scripts are added or removed: the command is split into arguments, the
program must be a Python interpreter and its script argument must be exactly that adapter path. A command
that only mentions the path (`echo <path>`, `python <path>.backup`) is kept, and so are other entries,
including neighbours in the same group. To remove:

```bash
python tools/install_hooks.py --project /path/to/your/project --remove
```

`--global` does the same for `~/.claude/settings.json`. The settings snippet to copy by hand is
`keel/hooks/settings.snippet.json`. Print your scoreboard once a session has run:

```bash
python -m keel panel --ledger ~/.keel/<slug>-<hash>/ledger.jsonl --days 14
```

## Quick start (commands that were actually run)

A task contract in an empty ledger. The first `verify` fails because the file does not exist yet, and
`close` refuses to record `verified`:

```bash
python -m keel open --ledger .keel/demo.jsonl --task T1 --goal "write a report" --dod "file_min_bytes:.keel/report.txt:20" --p 0.7
python -m keel verify --ledger .keel/demo.jsonl --task T1
python -m keel close --ledger .keel/demo.jsonl --task T1 --outcome verified
```

```
FAIL file_min_bytes:.keel/report.txt:20  <- ...\.keel\report.txt is missing
VERIFY: FAIL 0/1
REFUSED: outcome verified is not accepted: predicates not proven by the latest verify of this open: file_min_bytes:.keel/report.txt:20. Close as claimed or run verify.
```

Now do the work, verify again, close, and check the chain:

```bash
echo "revenue report for August" > .keel/report.txt
python -m keel verify --ledger .keel/demo.jsonl --task T1
python -m keel close --ledger .keel/demo.jsonl --task T1 --outcome verified
python -m keel audit --ledger .keel/demo.jsonl
```

```
OK   file_min_bytes:.keel/report.txt:20  <- ...\.keel\report.txt bytes=26 (need >= 20)
VERIFY: PASS 1/1
AUDIT: OK events 4, breaks 0
checkpoint (store it where the agent cannot write; the chain alone does not detect a cut tail): --expect-count 4 --expect-last-hash 3e4d...
```

The predicates run in the directory where `open` ran (pinned as an absolute path); `verify --cwd` with
another directory is refused.

A baseline from history (the `--projects-dir` default is `~/.claude/projects`):

```bash
python -m keel backfill --projects-dir ~/.claude/projects --out data/ledger
python -m keel report --ledger data/ledger
```

Record a verdict. The CLI always writes `src=cli`, which does not count in V1; `--by` is kept as a claim:

```bash
python -m keel verdict --ledger .keel/demo.jsonl --artifact out/tape.mp4 --verdict reject --reason "seams audible at 2:10" --by owner
```

Run everything: `python tests/run_checks.py` (selftest, pytest, hook end-to-end, contract-bypass,
state-race and ledger-concurrency probes). `pytest` is needed only for the pytest step.

## KEEL and ccusage

[ccusage](https://github.com/ryoppippi/ccusage) (MIT) reports tokens and cost in USD from local usage data
of many coding-agent CLIs. If your question is "what did this cost me", use ccusage; KEEL does not price
anything. KEEL records what the agent did (calls, failures, retries, edits without a read, claims and the
checks behind them) and derives behavioural metrics from that ledger. The two tools do not conflict.

## Known limitations

- **The chain is not a signature.** It shows that lines inside the chain were not edited after the fact,
  not that their content is true; anything the agent can write it can write falsely. A cut tail or a
  rebuilt file is caught only against a checkpoint you keep outside the agent's reach.
- **V1 counts claimed accepts.** The CLI cannot write an owner-channel verdict, but the ledger is not
  authenticated: a process that can write the file (or call `keel.contract.record_verdict` from Python) can
  append one. KEEL ships no owner-side integration.
- **Contracts check predicates, not intent.** Nothing detects "edited the test instead of the code" after
  the open; `manual:` predicates are closed by a word plus evidence text.
- **Strict Stop is a proxy.** It blocks a "done" with no successful tool result in the turn; it does not
  check that the result belongs to the claimed task (only a contract does).
- **Incident de-duplication.** Incidents found by the live PostToolUse/PostToolUseFailure detectors are
  reported once per (agent, class, key) within a session; the session state remembers the last 300 keys, so
  a repeat after that can be reported again. The key is the call for FLOOD and the message for FANOUT (in
  backfill the requestId; live there is no message id in the hook input, so Agent calls of one agent in one
  turn, each within 15 s of the previous one, count as one message: two messages less than 15 s apart are
  merged, one message whose Agent calls finish more than 15 s apart is split). `UNVERIFIED_CLAIM` (Stop),
  `REPEAT_ORDER` (UserPromptSubmit) and `MEMORY_LOST` (SessionStart) are written every time their condition
  holds. Interrupted calls do not count toward LOOP or TOOL_ERROR_BURST.
- **Durations.** `duration_ms` is used when the hook sends it; otherwise the gap since the previous event is
  used and marked `ms_approx`. Live events carry the completion time, backfill the start time, so STALL
  intervals of the two are shifted. There is no explicit start/end pair.
- **Layer cost.** V13 internal time stops before the ledger write (lock, write, fsync); the full call is
  measured only by a benchmark run. Each hook is a fresh Python process: roughly 200-350 ms wall clock per
  call on the test machine, of which about 20-40 ms is KEEL's own work.
- **Bands and point counts are experimental.** Thresholds were set on one user's history; the "8 points for
  calibration" and "12 points for precision" rules have no published analysis behind them.
- **Developed and tested locally on Windows only** (Windows 11, Python 3.11). Linux and macOS (the `fcntl`
  locking path) are exercised only by the CI workflow (`.github/workflows/checks.yml`, Windows, Linux and
  macOS on Python 3.11 and 3.12); see its latest run on GitHub for the current status. The tests drive the adapters with synthetic payloads shaped after the Claude Code
  hooks reference, not with recorded payloads from a live session.
- **Completion words are English only** (`keel/incidents.py`, `DONE_WORDS_RE`).
- **Ledgers written by 0.1.0** have no `open_id`/root in contracts and no `tool_use_id` in tool calls. Their
  contracts are replayed with the same in-order rules as new ones, matched by hash only (there is no
  `open_id`): a failed verify after a passed one cancels it, so V2 of an old ledger can be lower than 0.1.0
  reported. Their tool calls are not de-duplicated. Session state written by 0.1.0 is migrated on first
  use (its reads count for every agent of the session). A setup installed with 0.1.0 has no
  PostToolUseFailure registration: re-run the installer.
- Packaging metadata is in `pyproject.toml`, but the package is not on PyPI and `pip install` was not
  tested; run it from a clone.

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
