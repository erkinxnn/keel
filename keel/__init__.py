# -*- coding: utf-8 -*-
"""KEEL: a base layer for AI coding agents, built on Claude Code hooks.

The keel is the part of a ship below the deck: not the engine and not the sail, but the
structure that keeps it from capsizing. The layer produces nothing itself. It does four things:

1. keeps an append-only, hash-chained LEDGER of events: what the agent did, what it cost,
   what it claimed, what was proven, and what the human owner said;
2. computes VITAL SIGNS (vitals) only from ledger events, never from opinion;
3. recognises a REPEAT of a known incident class before it costs money again;
4. shows the agent its own SCOREBOARD at session start and before it says "done".

Laws of the layer:

- NO DATA instead of zero: where there are no events, the board prints "NO DATA" and what to
  start recording.
- Count events and artifacts, not the agent's report.
- A threshold without a recorded source has no right to fail anyone: it is reference only.
- The layer builds no walls: by default it advises and counts, and blocks only a "done" that
  comes with no evidence, and only when asked to (KEEL_STRICT=1).
- The layer measures its own cost (latency, bytes injected into context) and prints it beside
  the metrics.

Standard library only, Python 3.11+. No dependencies.
"""
from __future__ import annotations

__version__ = "0.2.1"
