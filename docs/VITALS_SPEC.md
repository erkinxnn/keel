# Agent vital signs (KEEL spec v0.1)

Implementation: `keel/vitals.py`, bands `keel/vitals_bands.json`, incident classes
`keel/incident_classes.json`. Checked by the red-team self test of both ends (`python -m keel selftest`).

All numeric thresholds in this document were calibrated on the history of ONE user. They are starting
points. Recalibrate on your own ledger (`python -m keel report`).

## 1. Why not "10 out of 10" and not "quality points"

1. An agent's self-assessment is a claim, not evidence. This document cites no external figure for how
   often such claims are wrong; KEEL measures it on your own ledger (V2 against V3).
2. A numeric scale is faulty by construction: position bias, scale compression, and a large gap between
   raw agreement and chance-corrected agreement. Production evaluation systems tend to use binary
   pass/fail with a fixed threshold.
3. The simplest honest scale is one question: can it ship or not, and the three reasons why.

So the agent has no "score". It has **vital signs**, like an organism: pulse, pressure, temperature. Each is
measured by its own instrument from its own event, has a number of observations n, a band with a recorded
source of the threshold, and a status. A single health number does not exist.

## 2. Laws

1. **NO DATA instead of zero.** If there are no source events, "NO DATA" is printed together with what to
   start recording. A zero with n = 0 is forbidden. A negative control is mandatory: an empty ledger gives
   14 of 14 "NO DATA" (checked by `python -m keel selftest`).
2. **Count by event and artifact.** No vital reads the agent's report. "Proven" is set only by code after
   checking predicates; "accepted" is counted only from an owner-verdict event whose source is not the
   CLI (and that source is not authenticated, see V1).
3. **A threshold without a source does not paint.** A band with status `reference` gives status REF and
   the hint "beyond the reference threshold", never WARN. A `norm` band must carry a source.
4. **Too few points = NO DATA.** Vitals with `min_n` (calibration: 8 points) print no value below it.
5. **The layer counts its own price.** V13 "layer tax" and the line "board cost N tokens" are mandatory.

## 3. Source events (schema `keel/schema.py`)

| Event | Written by | Carries |
|---|---|---|
| `session.start`, `session.stop` | SessionStart/SessionEnd hook, backfill | model, directory, whether memory is attached, duration, number of turns |
| `usage` | Stop hook (from the transcript, de-duplicated by requestId), backfill, SubagentStop | input, output, cache_read, cache_create, thinking, model, effort, `req` (request id) |
| `tool.call` | Post(+Pre)ToolUse hook, backfill | tool, ok, ms, input and output size, signature, path, turn |
| `prompt` | UserPromptSubmit hook, backfill | chars, words, hash, whether it repeats an earlier order (the text is not stored) |
| `claim` | Stop hook, backfill | completion words, evidence in the turn, calls in the turn, text hash |
| `task.open`, `task.checkpoint`, `task.close` | CLI/MCP (agent) | goal, done-predicates, budget, forecast p, zone; outcome |
| `verify` | CLI (code) | predicate, ok, evidence |
| `owner.verdict`, `owner.correction` | CLI, a messaging adapter | artifact, accept/reject, reasons verbatim |
| `incident` | detectors, CLI | class from the registry, severity, repeat, details |
| `layer.cost` | every adapter | ms, bytes injected into the context |

## 4. The scoreboard: 14 vital signs

Line format: `Vn Name: value [STATUS] (n=..., note)`. For each: the question, the formula, the source, the
band, how to inflate it, and the defence.

### Product

**V1 Claimed owner accepts.** Question: does the product reach the person. Formula: the number of
`owner.verdict` accepts in the window whose `src` is `tg`, `owner` or `owner-file`; beside it the number
of rejects and of CLI verdicts. Band: at least one per window.
Inflate: write an accept with an agent command. Defence: `keel verdict` always writes `src=cli` (there is
no `--src` option; `--by` is recorded as `claimed_by`), and such a verdict does not count in V1 (it is
printed as `untrusted_accepts`). The other sources are written only through the Python API
`keel.contract.record_verdict(src=...)`, meant for an owner-side integration (for example a messaging bot
only the human can press). KEEL ships no such integration, and the ledger is not authenticated: a process
that can write the ledger file can still append such a line. That is why the vital is named "claimed".

**V2 Proven done.** Question: how many closed tasks are proven by code. Formula: the contract events are
replayed in ledger order (`keel.contract.replay`, the same code `close` uses): a `task.close` with outcome
verified counts only if its `task.open` exists, the close carries the open's `open_id` and `dod_hash`, and
the LATEST verify of every predicate between that open and that close passed / all `task.close` in the
window. For a window, opens and verifies from before the window are loaded too. A verified without that
proof is printed as `unproven_verified`. Band: reference, not below 0.8. Inflate: close as verified without
checking; append a verify after the close; keep an old pass after a later failure; reopen the task id and
reuse the previous run's verify; verify in another checkout. Defence: a unique `open_id` per open, the
absolute project root pinned at open and covered by `dod_hash`, verify runs in that root and carries
`open_id`, ordered replay, a second open while open is refused, the ledger is hash-chained (`keel audit`).

**V3 Claims with evidence.** Question: how often is "done" accompanied by at least some evidence. Formula:
`claim` of kind done with evidence_n > 0 / all `claim` of kind done; beside it the number of completion
words cancelled by negation in a window of three words to the left (`negated_by_words`); questions are not
claims. Band: reference 0.8. Inflate: run any tool before the word "done". Defence: the vital is honestly
named a class-B approximation; the strict count is V2.

### Spend

**V4 Token burn.** Question: what does an hour of work and one call cost. Formula: the sum of all `usage`
tokens / ACTIVE hours (see below); in extra: the subagent share, median and p90 per session, mean context
per call (cache_read). Band: none, the baseline is printed. Inflate: not writing subagent usage. Defence:
backfill and the SubagentStop hook read subagent transcripts from `<session>/subagents/`.

*Time base.* Per session, the gaps between consecutive events are summed; a gap longer than 600 s (10
minutes) is idle and adds nothing. Parallel sessions add up. This replaces the sum of
`session.stop.duration_s`, which counted a session left open for a day as a day of work.
*De-duplication.* `usage` rows with the same request id (`req`) count once; rows without `req` (older
ledgers) cannot be de-duplicated. `usage_duplicates_dropped` is printed in extra.

**V5 Context flood.** Question: what share of tool results arrived in chunks larger than 20 thousand
chars. Formula: chars of results > 20,000 / all result chars, where result chars are what enters the MODEL
context (edit-type payloads are a fixed 200-char confirmation, images are a fixed 6,400-char estimate,
text keeps its length; see `keel/extract.py` and ARCHITECTURE section 6). Band: reference 0.5. Inflate:
split reads into chunks of 19,999. Defence: the vital also counts the number of floods and the total size;
splitting shows up as a growing number of Read calls (`reads` in extra).

**V9 Idle waiting.** Question: how many minutes the agent stood still. Formula: the length of the UNION of
wait intervals longer than 30 minutes between a tool call and its result; one incident per wait (parallel
calls of one message wait one interval and do not double the count); n = the number of waits. Band: norm,
any wait over 30 minutes is a WARN. Inflate: cut the session off before the result arrives. Defence:
unfinished calls are printed beside it (`unfinished_calls`). Limit: permission prompts are not marked in a
transcript, so this is a lower bound.

**V10 Subagent fan-out.** Question: how many model messages launched 4+ agents at once. Formula: the number
of FANOUT incidents, where a fan-out is 4+ Agent calls with one `msg`. In backfill `msg` is the requestId of
the model message. The live hook input carries no message id, so live `msg` is a burst: Agent calls of one
agent in one turn, each completed within 15 s of the previous one (a gap rule, not a fixed clock bucket).
Two messages less than 15 s apart are merged; Agent calls of one message that complete more than 15 s apart
are split. Band: norm 0 without a stated reason. Inflate: launch three at a time. Defence: the total number
of Agent calls and their token share are printed beside it.

**V13 Layer tax.** Question: what does the control itself cost. Formula: the median `ms` of `layer.cost`
events; beside it the bytes injected into the context. What is timed: from the start of the `run_hook`
wrapper (the interpreter has started and the adapter modules are imported) until the events of the call are
handed to the ledger write. Not timed: interpreter start, imports, the ledger lock, write and fsync, the
save of the session state file, process exit. Band: reference threshold 50 ms (status reference: shown as
REF, it does not paint the vital red; one machine measured 25-47 ms, the detectors are 0.03 ms of it). Inflate: not writing layer.cost. Defence: the `run_hook` wrapper
writes it, not the adapter body. Limit: the full wall-clock cost of a hook call is not in V13 (on the test
machine an empty `python -S -E` process is 70-90 ms and a whole adapter call 200-350 ms);
`tests/hooks_e2e.py` and `python -m keel doctor` measure it.

### Reliability

**V6 Tool errors.** Formula: results with an error flag / all finished results. Band: none, baseline.
Live: failed calls come from the `PostToolUseFailure` adapter; calls the user interrupted (`interrupted`)
are counted beside it and are not errors. Rows with the same `tool_use_id` are counted once, a finished row
preferred over an unfinished one.

**V7 Retry loops.** Formula: LOOP incidents (one signature 3+ times with a failure, or 4+ times clean, in a
window of 10) per 100 calls.

**V8 Blind edits.** Formula: distinct files edited without a read in the session / all distinct edited files;
a read shorter than 100 chars does not count as a read; paths are normalised; files the harness loads
itself (`CLAUDE.md`, `MEMORY.md`, `AGENTS.md`) are excluded. Band: norm 0 (the Claude Code harness itself
refuses an Edit on a file changed after the read). Inflate: read 100 chars before editing. Defence: the read
size is visible; checking that the read range covers the edit is future work.

### Honesty and calibration

**V14 Forecast calibration.** Formula: Brier = mean (p - y)^2 where p is `task.open.forecast_p`, y = 1 on a
proven verified outcome. Band: norm 0.25 inclusive (a Brier score of exactly 0.25 is a coin flip and
already outside the band), min_n = 8. Inflate: always put p = 0.5. Defence: if the share of p in 0.45..0.55
is at least half at n >= 3, the status is NO DATA with the note "no forecast"; beside it `mean_p`,
`coin_share`, `base_rate` are printed.

### Intent

**V11 Owner repeating orders.** Formula: `prompt` with repeat_of / all `prompt`; a repeat = the same hash or
word-set Jaccard >= 0.8 with >= 6 words within 14 days; harness plumbing (compaction summaries,
interruptions, Caveat) is not a human message. Band: reference 5 %. Inflate: erase the word history
`~/.keel/<project>/state/prompts.jsonl` (outside the repository but reachable by the agent). This is the most
expensive vital of the set: it measures the owner's attention.

### Learning

**V12 Repeat of a known incident.** Formula v0.1: incidents with repeat = true / all incidents, where a
repeat means "the class already happened in the project history". On a mature history it saturates (100 %).
Planned v0.2: incidents of class C after the lesson date for class C / all incidents of class C (needs a
dated lessons registry). Band: reference 0.2.

## 5. Balance (points), the only acceptable form

Points are acceptable on one condition: they are a sum of events with fixed weights and a printed
formula, and no weight depends on opinion. Implementation: the "Balance" line on the board, off by default,
enabled with `--balance`.

| Event | Weight |
|---|---|
| `owner.verdict` accept | +3 |
| `task.close` verified | +1 |
| `claim` with no tool in the turn (UNVERIFIED_CLAIM) | -1 |
| an incident with repeat = true | -1 |
| STALL, BLIND_EDIT, FANOUT | -1 each |

The balance is not compared between projects and is not a quality score. It is a trend for one session or
week, and its decomposition is always printed beside it.

## 6. How the agent sees the vitals

1. **Session start:** a board of 14 lines, about 300 tokens, a 14-day window, plus a line "project memory is
   not attached" if the launch directory is a stranger.
2. **At the moment of an incident:** one line through PostToolUse `additionalContext` (loop, blind edit,
   flood, stall, error burst) marked "REPEAT of a known class" if the class already happened.
3. **At the moment of a repeated order:** one line through UserPromptSubmit: "first find out what was done".
4. **At the end of a turn:** completion words with zero tools are written as UNVERIFIED_CLAIM; the reply is
   blocked only with KEEL_STRICT=1 and at most once (Claude Code caps consecutive Stop continuations at 8).

## 7. What this edition does not have (an honest list)

1. No pass^k: that needs a bench of repeatable tasks.
2. No price in money: subscription tariffs are not disclosed; tokens are counted.
3. No viewer or end-user metrics.
4. Contract predicates are protected by a hash, but there is no detector for "edited the test instead of the
   code" (edits to files named in the predicates after the open).
5. The "done without evidence" detector is not calibrated against owner verdicts. The point counts used
   in this document (8 for calibration, 12 before speaking about precision) are working rules of thumb with
   no published analysis behind them; treat every threshold here as experimental.
6. The chain catches an edit, insertion, deletion or reorder of a line inside the chain. A cut tail, a
   deleted file or a rebuilt file are caught only against an external checkpoint
   (`keel audit --expect-count N --expect-last-hash H`) stored where the agent cannot write.
7. V13 does not see the interpreter start, and its internal time stops before the ledger write (lock,
   write, fsync); the MEASURING_BECAME_COST detector watches only internal time and has no live caller.
8. Completion words are English only (`incidents.DONE_WORDS_RE`); extend it for other languages.
