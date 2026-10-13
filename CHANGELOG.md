# Changelog

## 0.2.1 - 2026-10-13

Second independent review. Every fix has a regression test in `tests/test_regressions_021.py` that failed
on 0.2.0.

Data safety and correctness:

- `export` no longer can truncate the source ledger: it refuses an output that is the same file (same path,
  or a hard link / another path to the same file, checked with `os.path.samefile`), reads the whole input
  before writing, and writes through a temporary file renamed over the output.
- Tool-call de-duplication by `tool_use_id` keeps a finished row (`ok` true/false) over an unfinished one
  (`ok` null), whole, so V6 no longer reports a failed call as unfinished.
- PostToolUse / PostToolUseFailure write the `tool.call` inside the session state lock and mark its
  `tool_use_id` as seen only after the write succeeded; a failed write no longer hides the call from the
  SubagentStop import. A live call whose id SubagentStop already imported is not written a second time.
  SubagentStop also writes first and marks after.
- The installer recognises a KEEL entry only when the command executes one of this checkout's adapter
  scripts: the command is split into arguments (POSIX shell rules, or double-quote grouping on Windows), the
  program must be a Python interpreter and its script argument must equal an adapter path after
  `abspath`/`normcase`. `echo "<adapter>"` and `python "<adapter>.backup"` are no longer removed.
- `close` checks the latest verify state and writes the close in one critical section under the ledger's
  append lock (`Ledger.lock()` / `Ledger.append_locked()`); `open` does the same for its "already open"
  check. A late close that names another `open_id` no longer ends the current open in the replay.

Detectors and metrics:

- Interrupted calls (`interrupted: true`) do not count toward LOOP or TOOL_ERROR_BURST.
- The scoreboard prints `NO DATA` in the value column whenever the status is NO DATA (it printed `0 min`).
- The BLIND_EDIT detector keeps reads per (session, agent), also in backfill.
- Live FANOUT groups Agent calls of one agent in one turn that complete within 15 s of the previous one (a
  gap rule) instead of fixed 15 s clock buckets, so a burst is no longer split at a bucket boundary. The hook
  input has no message id; the limit is documented.
- Corrupted incident details (`detail` null, a string, `minutes` not a number) no longer crash `vitals`;
  `schema.safe_int` / new `schema.safe_float` return the default for inf and nan.

State and storage:

- Session state written by 0.1.0 (`read_paths`, `recent`) is migrated into the per-agent structure; its
  reads stay valid for every agent of the session. The 40-agent cap evicts only subagents, never the head.
- `state/prompts.jsonl` is rewritten under a lock on every prompt without the entries older than 14 days.
- Project folders are named `<slug>-<10 hex of SHA-256 of the absolute path>`, so `a_b` and `a-b` no longer
  share a ledger. A 0.1.0/0.2.0 folder (bare slug) is still used when its `project.json` names the same
  directory.
- `contract.proven_predicates(opened=...)` now returns the results of that open (it ignored the argument).

Docs and tests:

- VITALS_SPEC V13 states the 50 ms band of `vitals_bands.json` and exactly what is timed; V6 and V10 updated.
- README claims rewritten to match the code (per-directory ledger, NO DATA, close/verify, SubagentStop
  de-duplication, prompt retention, installer matching, FANOUT grouping, incident de-duplication, 0.1.0
  compatibility).
- `tests/hooks_e2e.py` requires exactly one FANOUT for six Agent calls (zero failed silently before).

## 0.2.0 - 2026-10-13

First independent review.

- The CLI always records verdicts with `src=cli`; owner-channel sources only through the Python API.
- Each `task.open` gets a unique `open_id` and pins the absolute project root, covered by `dod_hash`;
  re-opening an open task is refused; `verify` refuses another `cwd`; `close --outcome verified` and V2
  replay the ledger in order (a verify after the close, a failure after a pass, or a verify of an earlier
  run does not count).
- The installer keeps foreign hook entries and their groups, makes a backup and writes atomically.
- Audit accepts an external checkpoint (`--expect-count`, `--expect-last-hash`); the integrity and privacy
  limits are documented; `export` redacts text-bearing fields.
- New PostToolUseFailure adapter: failed and interrupted calls reach the ledger.
- Subagent tool calls recorded live and again from the subagent transcript are de-duplicated by
  `tool_use_id`; per-agent live state for the blind-edit detector.
- Selftest path case folding follows the platform (CI on Linux and macOS).

## 0.1.0 - 2026-10-13

First public version.

- Hash-chained JSONL event ledger per project, written outside the repository.
- Claude Code hook adapters (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, SubagentStop, Stop,
  SessionEnd) and a backfill from existing transcripts.
- Incident detectors (loop, flood, stall, blind edit, error burst, fan-out, unverified claim, repeat order,
  memory lost) and 14 vital signs with sourced bands; NO DATA instead of zero.
- Task contracts with done-predicates, verify and close; owner verdicts.
- Selftest, hook end-to-end probe and adversarial probes.
