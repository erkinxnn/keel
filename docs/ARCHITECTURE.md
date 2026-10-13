# KEEL: architecture of a base layer for agents

Version 0.1. Code: `keel/`. Checks: `python -m keel selftest`, `python tests/hooks_e2e.py`, `python tests/run_checks.py`.

## 1. Where the layer sits

```
owner (human)  <---- verdict (accept/reject, reasons verbatim) ----  messaging bot / CLI
   |
   v
agent (Claude Code, or any MCP client)      <---- scoreboard, hints, refusal of "verified without verify"
   |         ^
   |  hooks / MCP / CLI
   v         |
+---------------------------- KEEL ----------------------------+
|  contract (task.open/close)   verification (verify predicates) |
|  incident detectors           incident class registry          |
|  vital signs                  scoreboard (panel / report)      |
|                     event ledger (JSONL, hash-chained)         |
+---------------------------------------------------------------+
   ^
   |  backfill: Claude Code transcripts (~/.claude/projects)
```

The layer sits below the agents and above the runtime. It does not orchestrate, choose a model or
write code. It does four things: writes a ledger, checks claims with code, recognises a repeat of
an incident, shows a scoreboard.

## 2. Components (implemented in v0.1)

| Component | File | What it does | Check |
|---|---|---|---|
| Event schema | `keel/schema.py` | 14 event types, required fields, shape and type validation of numeric fields; a malformed event is not written | selftest "schema rejects bad event", "schema rejects bad int" |
| Ledger | `keel/ledger.py` | append-only JSONL under an inter-process lock, one write per batch, fsync; hash chain `prev`/`h`, `audit()` catches an edited, inserted or deleted line; streaming read, corrupt lines are counted | selftest "concurrent append loses nothing" (8 processes), "audit catches tamper" |
| One extraction ruler | `keel/extract.py` | result length (what enters the MODEL context, see section 6), error flag, input size, path, path and command normalisation, identical for history and live hooks | selftest "signature ignores shell whitespace", the `flood:` cases, e2e "string error response is error" |
| Incident registry | `keel/incident_classes.json` | 16 classes with definition, severity, detection mode and rationale | selftest |
| Detectors | `keel/incidents.py` | LOOP, BLIND_EDIT, FLOOD, STALL, TOOL_ERROR_BURST, FANOUT, UNVERIFIED_CLAIM, REPEAT_ORDER, MEMORY_LOST, MEASURING_BECAME_COST; pure functions over a list of events; a repeat is set from history | selftest, both ends of each detector |
| Vitals | `keel/vitals.py` | 14 vital signs with n, status and band; NO DATA when there is no source | selftest, negative control |
| Bands | `keel/vitals_bands.json` | threshold + source + status norm/reference | selftest "bands have source" |
| Contract | `keel/contract.py` | open/verify/close/verdict; 7 predicate types; predicate hash at open, verify carries the hash, verified only after verify of every predicate with the same hash; a reopen is refused; a verdict has a source, trusted ones are `tg`, `owner`, `owner-file` | selftest "reopen refused", "forged verify with other hash refused", "V1 cli accept -> NO DATA"; `tests/adversarial/a2_contract_bypass.py` |
| Scoreboard | `keel/scoreboard.py` | panel (<= 20 lines, prints its own cost in tokens) and report (markdown) | e2e |
| Backfill | `keel/backfill.py` | Claude Code transcripts into a ledger, streaming; subagents from `<session>/subagents/`; usage de-duplicated by requestId; the owner's text is not copied | selftest on a synthetic transcript |
| CLI | `keel/__main__.py` | backfill, vitals, panel, report, audit, doctor, open, verify, close, verdict, incident, classes, selftest | e2e |
| Hook adapters | `keel/hooks/*.py` | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse (matcher `*`), SubagentStop, Stop (`last_assistant_message`, at most one block per turn), SessionEnd; session state under a lock with a unique temp file; incident class cache `state/classes.json` | `tests/hooks_e2e.py`, selftest "concurrent state keeps all writers", `a8_state_race.py` |

## 3. Data flow of one turn

The ledger and state of each project live outside the repository: `~/.keel/<encoded cwd>/`
(`ledger.jsonl`, `state/`, `hooks.log`, `project.json`), so the ledger does not land in git
auto-sync or in the session's context. `KEEL_HOME` overrides `~/.keel`, `KEEL_LEDGER` overrides the file.

1. `UserPromptSubmit`: a `prompt` event (numbers and a hash); a repeat-order check by word sets over
   14 days; on a repeat one line goes into the agent's context.
2. `PreToolUse` (optional, not registered by the installer, to save one process per call): the call
   timestamp. Without it the duration is counted from the previous event of the session, which is
   enough for a stall longer than 30 minutes.
3. `PostToolUse` (matcher `*`): a `tool.call` event with duration and result size; the agent is marked
   by `agent_id` (head or subagent); detectors run over the tail of 12 calls; a new incident is
   written once and goes to the agent as one line.
4. `Stop`: the last turn is taken from the transcript: `usage` per new requestId, `claim` with an
   evidence count; "done" with zero tools = an incident, in strict mode a block with a reason (once).
5. `SessionEnd`: `session.stop` with the duration and the number of turns.
6. Every adapter writes `layer.cost` (ms, injected bytes) through the `run_hook` wrapper.

## 4. Failure-handling principles

| Principle | Failure class it comes from | Implementation |
|---|---|---|
| `python`, not `pythonw`; traceback to a side log | `pythonw` swallows a NameError | `run_hook` writes `hooks.log` on any error, exit code 0 |
| fail-open everywhere except money-like claims | fail-closed gates blocked 21-37 % of legitimate commands in one audit | adapters do not block; the one exception is KEEL_STRICT=1 on Stop, at most one block per turn |
| stdin and stdout explicitly utf-8 | a legacy code page turns non-ASCII letters into dashes | `_common.py` |
| a registry of registrations checked by an instrument | six hooks silently removed by one config edit stayed dead for weeks | `python -m keel doctor` checks the registration of the six adapters |
| a red-team self test of both ends for every detector | stub tests that cannot fail | `keel/selftest.py` |
| an instrument with no caller is not added | dead instruments pile up | every component has a caller in the table of section 2 |
| the ledger holds no owner text | privacy, portability | only numbers and hashes |

## 5. Portability

1. **The ledger** is JSONL with fields compatible with OpenTelemetry GenAI (`usage.input/output/cache_*`,
   `tool.call` as a span). An OTLP exporter is not implemented yet.
2. **The core knows nothing about Claude Code.** The hook adapters live separately; backfill separately;
   the core accepts events from anyone.
3. **No dependencies:** the Python 3.11 standard library.

## 6. The context-flood metric (what `out_bytes` means)

`out_bytes` is meant to measure what ENTERS THE MODEL'S CONTEXT, not what a hook payload carries.
The hook `tool_response` of Edit, Write, MultiEdit and NotebookEdit contains `originalFile`,
`oldString`, `newString`, `structuredPatch`: bookkeeping the model never sees (it receives a
one-line confirmation). Before the fix an audit found the following (chars counted vs about visible):
Edit 39,323 vs 205, Write 15,928 vs 208, Read 130,283 vs 5,954 (the Read figure included base64 image bytes).

`keel/extract.py` therefore counts:

* edit-type tools: a fixed `EDIT_CONFIRM_CHARS` = 200 (a plain string response, e.g. an error text, is kept as is);
* images (any block or hook response carrying `type: image` or base64 bytes): a fixed estimate of
  `IMAGE_TOKENS` = 1,600 tokens, times `CHARS_PER_TOKEN` = 4, i.e. 6,400 chars per image. Real cost is
  about width*height/750 tokens, capped near 1,600 for large images, so this is an upper-range estimate;
* text: the text length;
* numeric and boolean values inside a hook dict (`numLines` and the like) are bookkeeping and are skipped.

Transcript rows used by `backfill` already hold the model-visible content, so history and live hooks now
agree. Ledgers written by older versions contain the inflated live numbers; re-run backfill or ignore V5
before the upgrade date.

## 7. The token-rate metric (V4) and its time base

The denominator of V4 is ACTIVE time. Per session, the gaps between consecutive events are summed; a gap
longer than 600 s counts as idle and adds nothing. Sessions are measured separately and their active times
add up (parallel sessions can exceed 24 h per day). Before the fix the denominator was the sum of
`session.stop.duration_s`, which counts a session left open overnight as hours of work (in one audit:
a 25-hour session, 30.5 h used vs 70.3 active hours).

Duplicates: `usage` events carry the model request id in `req`. When a subagent transcript is parsed again
after it grew (SubagentStop fires more than once), its earlier usage rows are written again;
`keel.vitals` counts rows with the same `req` once and prints `usage_duplicates_dropped`. Rows written by
older versions have no `req` and cannot be de-duplicated.

## 8. Hook payloads and the Claude Code hooks reference

Checked against https://code.claude.com/docs/en/hooks (see the README, "Limits"). Field names used by the
adapters match: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `tool_name`, `tool_input`,
`tool_response`, `tool_use_id`, `prompt`, `source`, `reason`, `agent_id`, `agent_transcript_path`,
`last_assistant_message`, `stop_hook_active`; the output forms `hookSpecificOutput.additionalContext` and
top-level `decision: "block"` + `reason` match too.

Known gaps:

* **PostToolUse fires only for calls that succeed.** Failed calls go to a separate `PostToolUseFailure`
  event (fields `error`, `is_interrupt`, `duration_ms`), for which there is no adapter yet. In a live ledger
  `tool.call.ok=false` therefore appears only for string responses that start with "Error", so V6 (tool
  errors), LOOP-with-failure and TOOL_ERROR_BURST under-count live. History (backfill) is unaffected.
* PostToolUse and PostToolUseFailure now carry an optional `duration_ms`; the adapter still derives the
  duration from its own PreToolUse/previous-event bookkeeping.
* The nested `hookSpecificOutput.decision` form printed by `stop.py` in strict mode is not in the current
  reference (the top-level form is the documented one); it is kept from an earlier doc edition.

## 9. Cost of the layer

| What | ms (one Windows machine) |
|---|---|
| `python -c pass` | 255 |
| `python -S -E -c pass` | 99 |
| adapter, internal time (layer.cost median) | 25-40 |
| adapter, wall clock, cold start | 240-360 |

The layer prints V13 honestly, including in the red zone. If you cannot afford one process per call,
leave PreToolUse unregistered (the installer does) and keep only the per-turn events.

## 10. Privacy

The ledger holds no message text, file content or command output: only lengths, hashes, tool names and
paths. The word sets used to find repeated orders are kept in `state/prompts.jsonl` next to the ledger and
are never exported.
