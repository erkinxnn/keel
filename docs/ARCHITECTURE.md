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
| Ledger | `keel/ledger.py` | append-only JSONL under an inter-process lock, one write per batch, fsync; hash chain `prev`/`h`; `audit()` catches an edited line and a line inserted, deleted or moved inside the chain, a cut tail only against an external checkpoint (`--expect-count`, `--expect-last-hash`); the previous hash is found even after a record longer than 64 KiB; streaming read, corrupt lines are counted; `redact_event` for sharing | selftest "concurrent append loses nothing" (8 processes), "audit catches tamper" |
| One extraction ruler | `keel/extract.py` | result length (what enters the MODEL context, see section 6), error flag, input size, path, path and command normalisation, identical for history and live hooks | selftest "signature ignores shell whitespace", the `flood:` cases, e2e "string error response is error" |
| Incident registry | `keel/incident_classes.json` | 16 classes with definition, severity, detection mode and rationale | selftest |
| Detectors | `keel/incidents.py` | LOOP, BLIND_EDIT, FLOOD, STALL, TOOL_ERROR_BURST, FANOUT, UNVERIFIED_CLAIM, REPEAT_ORDER, MEMORY_LOST, MEASURING_BECAME_COST; pure functions over a list of events; a repeat is set from history | selftest, both ends of each detector |
| Vitals | `keel/vitals.py` | 14 vital signs with n, status and band; NO DATA when there is no source | selftest, negative control |
| Bands | `keel/vitals_bands.json` | threshold + source + status norm/reference | selftest "bands have source" |
| Contract | `keel/contract.py` | open/verify/close/verdict; 7 predicate types; each open has a unique `open_id` and pins the absolute project root, which `dod_hash` covers; verify runs in that root and carries `open_id`; `replay` walks contract events in ledger order and is shared by `close` and V2; a reopen while open is refused; the CLI always writes verdicts with `src=cli` | selftest "reopen refused", "forged verify with other hash refused", "V1 cli accept -> NO DATA"; `tests/adversarial/a2_contract_bypass.py` |
| Scoreboard | `keel/scoreboard.py` | panel (<= 20 lines, prints its own cost in tokens) and report (markdown) | e2e |
| Backfill | `keel/backfill.py` | Claude Code transcripts into a ledger, streaming; subagents from `<session>/subagents/`; usage de-duplicated by requestId; the owner's text is not copied | selftest on a synthetic transcript |
| CLI | `keel/__main__.py` | backfill, vitals, panel, report, audit, doctor, open, verify, close, verdict, incident, classes, selftest | e2e |
| Hook adapters | `keel/hooks/*.py` | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse and PostToolUseFailure (matcher `*`), SubagentStop, Stop (`last_assistant_message`, at most one block per turn), SessionEnd; session state under a lock with a unique temp file; incident class cache `state/classes.json` | `tests/hooks_e2e.py`, selftest "concurrent state keeps all writers", `a8_state_race.py` |

## 3. Data flow of one turn

The ledger and state of each project live outside the repository: `~/.keel/<encoded cwd>/`
(`ledger.jsonl`, `state/`, `hooks.log`, `project.json`), so the ledger does not land in git
auto-sync or in the session's context. `KEEL_HOME` overrides `~/.keel`, `KEEL_LEDGER` overrides the file.

1. `UserPromptSubmit`: a `prompt` event (numbers and a hash); a repeat-order check by word sets over
   14 days; on a repeat one line goes into the agent's context.
2. `PreToolUse` (optional, not registered by the installer, to save one process per call): the call
   timestamp. Without it the duration is counted from the previous event of the session, which is
   enough for a stall longer than 30 minutes.
3. `PostToolUse` (matcher `*`): a `tool.call` event with duration (`duration_ms` when the hook sends
   it) and result size; the agent is marked by `agent_id` (head or subagent); reads and the window of
   the last 12 calls are kept per agent; detectors run over that window; a new incident is written once
   and goes to the agent as one line. `PostToolUseFailure` (matcher `*`) does the same for a failed or
   interrupted call (`ok=false`, `interrupted`), without storing the error text.
4. `Stop`: the last turn is taken from the transcript: `usage` per new requestId, `claim` with an
   evidence count; "done" with no successful tool result in the turn = an incident, in strict mode a
   block with a reason (once). It does not check that the result belongs to a task: only a contract does.
   `SubagentStop` imports the subagent transcript and skips tool calls already recorded live (by
   `tool_use_id`) and usage rows already imported (by request id).
5. `SessionEnd`: `session.stop` with the duration and the number of turns.
6. Every adapter writes `layer.cost` (ms, injected bytes) through the `run_hook` wrapper.

## 4. Failure-handling principles

| Principle | Failure class it comes from | Implementation |
|---|---|---|
| `python`, not `pythonw`; traceback to a side log | `pythonw` swallows a NameError | `run_hook` writes `hooks.log` on any error, exit code 0 |
| fail-open everywhere except money-like claims | fail-closed gates blocked 21-37 % of legitimate commands in one audit | adapters do not block; the one exception is KEEL_STRICT=1 on Stop, at most one block per turn |
| stdin and stdout explicitly utf-8 | a legacy code page turns non-ASCII letters into dashes | `_common.py` |
| a registry of registrations checked by an instrument | six hooks silently removed by one config edit stayed dead for weeks | `python -m keel doctor` checks the registration of the seven adapters |
| a red-team self test of both ends for every detector | stub tests that cannot fail | `keel/selftest.py` |
| an instrument with no caller is not added | dead instruments pile up | every component has a caller in the table of section 2 |
| hook events hold no prompt or reply text | privacy, portability | numbers, hashes, tool names, file paths; contract and verdict events are different, see section 10 |

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

* **Failed calls** go to `PostToolUseFailure` (fields `error`, `is_interrupt`, `duration_ms`), handled by
  `post_tool_use_failure.py` (added in 0.2.0). A setup installed with 0.1.0 has no such registration: re-run
  `tools/install_hooks.py`. Calls blocked by a PreToolUse hook or denied by a permission rule never ran and
  are not recorded as tool calls.
* `duration_ms` is used when the hook sends it (it covers execution, not the permission prompt). Without it
  the duration comes from PreToolUse (optional) or from the previous event, marked `ms_approx`. Live `ts` is
  the completion time; backfill `ts` is the call start, so STALL intervals of the two are shifted.
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

What is stored depends on the event:

* **Hook events** (`prompt`, `tool.call`, `usage`, `claim`, `session.*`, `incident`, `layer.cost`): no prompt
  text, no reply text, no file content, no command output, no error text. They do hold lengths, hashes,
  tool names, file paths (`tool.call.path`, incident details), the launch directory (`session.start.cwd`),
  transcript paths, and the completion words found in a reply (`claim.words`, e.g. "done").
* **Contract and verdict events** (written by the CLI) hold what you give them, verbatim: the goal text, the
  predicates (paths, regexes, shell commands), the evidence of each verify (for `cmd_exit0` the last 300
  chars of the command's stdout, for `regex_in_file` up to 80 chars of the matched text), close evidence,
  verdict reasons and artifact names.
* **Session state** next to the ledger: `state/prompts.jsonl` keeps the SET OF WORDS of each prompt for 14
  days (to find repeated orders), which is close to the prompt text; `hooks.log` keeps adapter tracebacks.

Before sharing a ledger use `python -m keel export --ledger L --out X`: it masks goal, predicates, evidence,
verdict reasons and artifacts, incident details, paths and cwd as `redacted:<hash>` (equal values give equal
masks). The export keeps the original hashes and therefore no longer passes `audit`. Do not share `state/`.
