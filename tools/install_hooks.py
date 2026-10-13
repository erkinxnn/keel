# -*- coding: utf-8 -*-
"""Install and remove the KEEL adapters in Claude Code settings. The USER runs it, with one command.

    python tools/install_hooks.py --project <path>        only sessions started from that folder
    python tools/install_hooks.py --check --project <path> show what is registered there
    python tools/install_hooks.py --global                every Claude Code session on the machine
                                                          (any folder, subagents, headless runs)
    python tools/install_hooks.py --project <path> --remove   remove (or --global --remove)

What is touched: only hook ENTRIES whose command RUNS one of this checkout's adapter scripts: the
command is split into arguments (POSIX shell rules on Linux/macOS, double-quote grouping on Windows),
the program must be a Python interpreter, and its script argument must be exactly one of the paths
`keel/hooks/<adapter>.py` of this checkout (compared after abspath and normcase). A command that only
mentions such a path (`echo <path>`, `python <path>.backup`) is not KEEL's and is left alone. Other entries, including entries that
share a group with a KEEL adapter, are kept; a group is dropped only if it becomes empty.
Registrations that point to ANOTHER copy of KEEL are not recognised and are left alone.
Before any write a backup `settings.json.bak_keel_<date_time>` is made, and the new file is
written to a temporary file and renamed over the old one (atomic on the same volume).

Seven events are registered: SessionStart, UserPromptSubmit, PostToolUse (matcher *),
PostToolUseFailure (matcher *), SubagentStop, Stop, SessionEnd. PreToolUse is not registered, to
save one process per tool call.

The command is `"<this interpreter>" -S -E "<adapter>"`: the absolute path of the Python that ran
the installer is pinned, so the hooks do not depend on what `python` means in the session's PATH.
`-S` skips site-packages processing at start (tens of ms on every tool call on the test machine;
the layer uses only the standard library); `-E` detaches the adapter from PYTHONPATH.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "keel" / "hooks"

PLAN = [
    ("SessionStart", None, "session_start.py", 10, "KEEL: session scoreboard"),
    ("UserPromptSubmit", None, "user_prompt_submit.py", 5, "KEEL: order"),
    ("PostToolUse", "*", "post_tool_use.py", 5, "KEEL: ledger"),
    ("PostToolUseFailure", "*", "post_tool_use_failure.py", 5, "KEEL: failed call"),
    ("SubagentStop", None, "subagent_stop.py", 15, "KEEL: subagent to ledger"),
    ("Stop", None, "stop.py", 15, "KEEL: evidence on done"),
    ("SessionEnd", None, "session_end.py", 5, "KEEL: end of session"),
]
ADAPTERS = sorted({p[2] for p in PLAN} | {"pre_tool_use.py"})


_PYTHON_EXE = re.compile(r"^(python|pythonw|py)([0-9][0-9.]*)?(\.exe)?$", re.IGNORECASE)
_LONG_OPTS_WITH_ARG = ("--check-hash-based-pycs",)


def _split_windows(cmd: str) -> list[str]:
    """Whitespace-separated arguments; a double-quoted span belongs to one argument (quotes removed).
    No escape characters: Windows paths keep their backslashes. Unbalanced quotes: no arguments."""
    out: list[str] = []
    cur: list[str] = []
    quoted = started = False
    for ch in cmd:
        if ch == '"':
            quoted, started = not quoted, True
        elif ch.isspace() and not quoted:
            if started:
                out.append("".join(cur))
                cur, started = [], False
        else:
            cur.append(ch)
            started = True
    if quoted:
        return []
    if started:
        out.append("".join(cur))
    return out


def _split_command(cmd: str) -> list[str]:
    if os.name == "nt":
        return _split_windows(cmd)
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return []


def _script_of(cmd: str) -> str | None:
    """The script a hook command executes: `<python> [options] <script> ...`, else None.

    Interpreter options are read the way CPython reads them, including combined short flags
    (`-Sc`, `-Em`): any `-c` or `-m` anywhere in the options means inline code or a module, so the
    entry is not a script run and None is returned. `-W` and `-X` take a value (the rest of the same
    argument, else the next one); `--check-hash-based-pycs` takes the next argument."""
    args = _split_command(cmd)
    if len(args) < 2 or not _PYTHON_EXE.match(os.path.basename(args[0].replace("\\", "/"))):
        return None
    i = 1
    while i < len(args):
        a = args[i]
        if a == "-":
            return None  # the program comes from stdin
        if a.startswith("--"):
            i += 2 if a in _LONG_OPTS_WITH_ARG else 1
            continue
        if a.startswith("-"):
            skip = 1
            for j, ch in enumerate(a[1:], start=1):
                if ch in "cm":
                    return None  # inline code or a module, not a script (also inside -Sc, -Em, ...)
                if ch in "WX":
                    skip = 1 if a[j + 1:] else 2  # the value is the rest of this argument, else the next one
                    break
            i += skip
            continue
        return a
    return None


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _adapter_keys() -> dict[str, str]:
    return {_key(str(HOOKS / a)): a for a in ADAPTERS}


def _adapter_of(h: object) -> str | None:
    """The adapter file name if the entry runs one of this checkout's adapter scripts, else None."""
    if not isinstance(h, dict) or h.get("type", "command") != "command":
        return None
    script = _script_of(str(h.get("command") or ""))
    return _adapter_keys().get(_key(script)) if script else None


def _is_keel_hook(h: object) -> bool:
    """True only for a command entry that runs one of this checkout's adapter scripts."""
    return _adapter_of(h) is not None


def _keel_entries(groups: list) -> list[dict]:
    return [h for g in groups if isinstance(g, dict) for h in (g.get("hooks") or []) if _is_keel_hook(h)]


def _strip_keel(groups: list) -> tuple[list, int]:
    """Remove KEEL adapter entries, keep every other entry and the groups they live in."""
    out: list = []
    removed = 0
    for g in groups:
        if not isinstance(g, dict) or not isinstance(g.get("hooks"), list) or not g["hooks"]:
            out.append(g)
            continue
        keep = [h for h in g["hooks"] if not _is_keel_hook(h)]
        removed += len(g["hooks"]) - len(keep)
        if len(keep) == len(g["hooks"]):
            out.append(g)
        elif keep:
            out.append({**g, "hooks": keep})
    return out, removed


def _python() -> str:
    return str(Path(sys.executable).resolve())


def _settings_path(project: str | None) -> Path:
    if project:
        return Path(project).resolve() / ".claude" / "settings.json"
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "~").expanduser()
    return home / ".claude" / "settings.json"


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.keel_tmp_{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def check(path: Path) -> dict[str, int]:
    if not path.exists():
        print(f"{path}: no file")
        return {}
    cfg = json.loads(path.read_text(encoding="utf-8"))
    hooks = cfg.get("hooks", {})
    res = {}
    for ev, _m, script, *_r in PLAN:
        res[ev] = sum(1 for h in _keel_entries(hooks.get(ev, [])) if _adapter_of(h) == script)
    print(f"{path}:")
    for ev, n in res.items():
        print(f"  {ev:18s} {'registered' if n else 'no'}{' (duplicate x%d)' % n if n > 1 else ''}")
    others = sum(len(g.get("hooks") or []) for v in hooks.values() if isinstance(v, list) for g in v if isinstance(g, dict)) \
        - sum(len(_keel_entries(v)) for v in hooks.values() if isinstance(v, list))
    print(f"  other hook entries: {others}")
    slow = sorted({ev for ev, *_ in PLAN for h in _keel_entries(hooks.get(ev, [])) if "-S -E" not in str(h.get("command"))})
    if slow:
        print(f"  WARNING: registered without -S -E: {', '.join(slow)}: re-register with --project or --global")
    for ev, _m, script, *_r in PLAN:
        if not (HOOKS / script).exists():
            print(f"  WARNING: adapter file missing: {HOOKS / script}")
    return res


def install(path: Path, remove: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if path.exists():
        bak = path.with_name(f"settings.json.bak_keel_{dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}")
        shutil.copy2(path, bak)
        print(f"backup: {bak}")
    hooks = cfg.setdefault("hooks", {})
    changed = []
    py = _python()
    for ev, matcher, script, timeout, msg in PLAN:
        groups, removed = _strip_keel(hooks.get(ev, []))
        if remove:
            if removed:
                changed.append(f"-{ev}")
        else:
            g: dict = {"hooks": [{"type": "command", "command": f'"{py}" -S -E "{HOOKS / script}"', "timeout": timeout,
                                  "statusMessage": msg}]}
            if matcher:
                g["matcher"] = matcher
            groups.append(g)
            changed.append(f"+{ev}")
        if groups:
            hooks[ev] = groups
        else:
            hooks.pop(ev, None)
    if not hooks:
        cfg.pop("hooks", None)
    _write_atomic(path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    print(f"{path}: {' '.join(changed) or 'no changes'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="install the KEEL adapters into Claude Code settings")
    ap.add_argument("--global", dest="glob", action="store_true", help="globally: ~/.claude/settings.json")
    ap.add_argument("--project", help="for one project only: <path>/.claude/settings.json")
    ap.add_argument("--remove", action="store_true", help="remove the KEEL adapters")
    ap.add_argument("--check", action="store_true", help="only show the state (of --project if given, and of the global file)")
    a = ap.parse_args()
    if not (a.glob or a.project or a.check):
        ap.error("specify --global, --project <path> or --check")
    if a.check:
        if a.project:
            check(_settings_path(a.project))
        check(_settings_path(None))
        return 0
    if a.glob:
        install(_settings_path(None), a.remove)
        check(_settings_path(None))
    if a.project:
        install(_settings_path(a.project), a.remove)
        check(_settings_path(a.project))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
