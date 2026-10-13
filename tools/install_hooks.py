# -*- coding: utf-8 -*-
"""Install and remove the KEEL adapters in Claude Code settings. The USER runs it, with one command.

    python tools/install_hooks.py --check                 show what is registered now
    python tools/install_hooks.py --global                every Claude Code session on the machine
                                                          (any folder, subagents, headless runs)
    python tools/install_hooks.py --project <path>        only sessions started from that folder
    python tools/install_hooks.py --global --remove       remove globally (or --project ... --remove)

Before any write a backup `settings.json.bak_keel_<date_time>` is made. Other people's hooks are
not touched: only groups whose command points into `keel/hooks` are added or removed.
Six events are registered: SessionStart, UserPromptSubmit, PostToolUse (matcher *), SubagentStop,
Stop, SessionEnd. PreToolUse is not registered, to save one process per tool call.

The command is run as `python -S -E`: processing site packages at interpreter start costs
tens of milliseconds on EVERY tool call (measured on one machine: an empty `python` process
172/240 ms vs `python -S -E` 76/87 ms, median of an alternating run), and the layer does not
need site at all: it has zero dependencies outside the standard library. `-E` also detaches the
adapter from the session's PYTHONPATH.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "keel" / "hooks"
MARK = "keel"

PLAN = [
    ("SessionStart", None, "session_start.py", 10, "KEEL: session scoreboard"),
    ("UserPromptSubmit", None, "user_prompt_submit.py", 5, "KEEL: order"),
    ("PostToolUse", "*", "post_tool_use.py", 5, "KEEL: ledger"),
    ("SubagentStop", None, "subagent_stop.py", 15, "KEEL: subagent to ledger"),
    ("Stop", None, "stop.py", 15, "KEEL: evidence on done"),
    ("SessionEnd", None, "session_end.py", 5, "KEEL: end of session"),
]


def _is_keel(group: dict) -> bool:
    text = json.dumps(group, ensure_ascii=False).replace("\\\\", "\\").lower()
    return "keel" in text and "hooks" in text


def _settings_path(project: str | None) -> Path:
    if project:
        return Path(project).resolve() / ".claude" / "settings.json"
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "~").expanduser()
    return home / ".claude" / "settings.json"


def check(path: Path) -> dict[str, int]:
    if not path.exists():
        print(f"{path}: no file")
        return {}
    cfg = json.loads(path.read_text(encoding="utf-8"))
    hooks = cfg.get("hooks", {})
    res = {}
    for ev, *_ in PLAN:
        n = sum(1 for g in hooks.get(ev, []) if _is_keel(g))
        res[ev] = n
    print(f"{path}:")
    for ev, n in res.items():
        print(f"  {ev:18s} {'registered' if n else 'no'}{' (duplicate x%d)' % n if n > 1 else ''}")
    others = sum(len(v) for v in hooks.values()) - sum(res.values())
    print(f"  other hook groups: {others}")
    slow = [ev for ev, *_ in PLAN
            for g in hooks.get(ev, []) if _is_keel(g) and "-S -E" not in json.dumps(g, ensure_ascii=False)]
    if slow:
        print(f"  WARNING: registered without -S -E: {', '.join(sorted(set(slow)))}: that is tens of extra ms "
              f"on every call. Re-register: python tools/install_hooks.py --global")
    return res


def install(path: Path, remove: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if path.exists():
        bak = path.with_name(f"settings.json.bak_keel_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(path, bak)
        print(f"backup: {bak}")
    hooks = cfg.setdefault("hooks", {})
    changed = []
    for ev, matcher, script, timeout, msg in PLAN:
        groups = hooks.setdefault(ev, [])
        before = len(groups)
        groups[:] = [g for g in groups if not _is_keel(g)]
        if remove:
            if len(groups) != before:
                changed.append(f"-{ev}")
        else:
            g = {"hooks": [{"type": "command", "command": f'python -S -E "{HOOKS / script}"', "timeout": timeout, "statusMessage": msg}]}
            if matcher:
                g["matcher"] = matcher
            groups.append(g)
            changed.append(f"+{ev}")
        if not groups:
            hooks.pop(ev, None)
    if not hooks:
        cfg.pop("hooks", None)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{path}: {' '.join(changed) or 'no changes'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="install the KEEL adapters into Claude Code settings")
    ap.add_argument("--global", dest="glob", action="store_true", help="globally: ~/.claude/settings.json")
    ap.add_argument("--project", help="for one project only: <path>/.claude/settings.json")
    ap.add_argument("--remove", action="store_true", help="remove the KEEL adapters")
    ap.add_argument("--check", action="store_true", help="only show the state")
    a = ap.parse_args()
    if not (a.glob or a.project or a.check):
        ap.error("specify --global, --project <path> or --check")
    if a.check:
        check(_settings_path(None))
        check(_settings_path(a.project) if a.project else ROOT / ".claude" / "settings.json")
        return 0
    if a.glob:
        install(_settings_path(None), a.remove)
    if a.project:
        install(_settings_path(a.project), a.remove)
    check(_settings_path(None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
