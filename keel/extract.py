# -*- coding: utf-8 -*-
"""One ruler for the live hook and for history.

This module holds the functions that both `backfill` and the hook adapters use to extract the
same fields from different representations of the same thing: a tool result, a tool input, a
text. If a field is computed two different ways, a "before and after" snapshot cannot be
compared.

What "output size" means (the flood metric, V5 and the FLOOD incident)
----------------------------------------------------------------------
`out_bytes` (the field keeps its historical name; the unit is CHARACTERS) is meant to measure
what ENTERS THE MODEL'S CONTEXT, not what the hook payload happens to carry. The two differ a lot:

* Edit / Write / MultiEdit / NotebookEdit. The PostToolUse `tool_response` of these tools holds
  `originalFile`, `oldString`, `newString`, `structuredPatch` and similar bookkeeping. The model
  never sees any of it: it receives a one-line confirmation. An audit of one real history found
  Edit counted at 39,323 chars against about 205 visible, and Write at 15,928 against about 208.
  Such tools are therefore counted as a short fixed confirmation (EDIT_CONFIRM_CHARS).
* Images (Read of a PNG/JPG, screenshots). The payload carries the base64 bytes (hundreds of KB),
  but the model pays a bounded number of tokens per image. Every image is counted as a fixed
  estimate: IMAGE_TOKENS (1,600, roughly the cap for one large image; real cost is about
  width*height/750 tokens) times CHARS_PER_TOKEN (4, a common rule of thumb for English text),
  i.e. IMAGE_CHARS = 6,400 chars. It is an estimate, not a measurement.
* Text results keep their text length.

Transcript rows (used by backfill) already store the model-visible content, so the same
`content_len` applies to them unchanged.
"""
from __future__ import annotations

import json
import os

TYPE_CHECKING = False  # typing costs 7 ms per import; annotations are deferred (from __future__ import annotations)
if TYPE_CHECKING:  # pragma: no cover
    from typing import Any

PATH_KEYS = ("file_path", "notebook_path", "path")

CHARS_PER_TOKEN = 4          # rule of thumb used to turn a token estimate into "chars"
IMAGE_TOKENS = 1600          # fixed estimate for one image block (upper range for a large image)
IMAGE_CHARS = IMAGE_TOKENS * CHARS_PER_TOKEN  # 6,400 chars per image
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
EDIT_CONFIRM_CHARS = 200     # the model sees a one-line confirmation, about 200 chars (measured 205 / 208)

# keys of a hook response dict that are bookkeeping, not model-visible text
_META_KEYS = ("filePath", "file_path", "type", "tool_use_id", "is_error", "isError", "interrupted", "isImage")

# whitespace folding is done with split/join: exactly what re.sub(r"\s+", " ").strip() does,
# but without importing re (measured: re costs 24 ms on every adapter call)


def _is_image(d: Any) -> bool:
    """An image payload: a content block or a hook `tool_response` that carries image bytes."""
    if not isinstance(d, dict):
        return False
    if d.get("type") == "image" or "base64" in d:
        return True
    src = d.get("source")
    if isinstance(src, dict) and src.get("type") == "base64":
        return True
    f = d.get("file")
    return isinstance(f, dict) and "base64" in f


def content_len(content: Any) -> int:
    """Length of a tool result in chars, the same for a transcript row and a hook dict.

    Images count as IMAGE_CHARS; numeric and boolean values inside a dict are bookkeeping
    counters (numLines, durations) and do not count; strings and text blocks count as written."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, bool) or isinstance(content, (int, float)):
        return len(str(content))
    if isinstance(content, list):
        n = 0
        for b in content:
            if isinstance(b, dict):
                if _is_image(b):
                    n += IMAGE_CHARS
                elif b.get("type") == "text":
                    n += len(b.get("text") or "")
                else:
                    n += content_len(b)
            else:
                n += content_len(b)
        return n
    if isinstance(content, dict):
        if _is_image(content):
            return IMAGE_CHARS
        # hook response dict: count only string-like values (stdout, stderr, content, text ...);
        # bookkeeping keys (paths, flags) and numeric counters do not inflate the count
        n = 0
        for k, v in content.items():
            if k in _META_KEYS or isinstance(v, (bool, int, float)):
                continue
            n += content_len(v)
        return n
    return len(str(content))


def tool_result_len(tool: str, resp: Any) -> int:
    """What a tool result adds to the model's context, in chars (the `out_bytes` field).

    Edit-type tools: the payload is bookkeeping, the model sees a short confirmation, so a
    constant. A plain string response (an error message or a confirmation text) is kept as is.
    Everything else goes through `content_len`."""
    if tool in EDIT_TOOLS:
        if isinstance(resp, str):
            return len(resp)
        return EDIT_CONFIRM_CHARS
    return content_len(resp)


def is_error(resp: Any) -> bool:
    """Result error: an is_error/isError flag, an interruption, or a string starting with Error."""
    if isinstance(resp, dict):
        if resp.get("is_error") is True or resp.get("isError") is True or resp.get("interrupted") is True:
            return True
        return False
    if isinstance(resp, str):
        head = resp.lstrip()[:40].lower()
        return head.startswith(("error", "traceback", "exception"))
    return False


def in_bytes(tool_input: Any) -> int:
    if tool_input is None:
        return 0
    try:
        return len(json.dumps(tool_input, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(tool_input))


def tool_path(tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for k in PATH_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def norm_path(p: str) -> str:
    return os.path.normcase(os.path.normpath(str(p).replace("\\", "/")))


def normalized_input(tool: str, tool_input: Any) -> Any:
    """Input for the call signature: whitespace in shell commands is collapsed (an extra space
    must not defeat the loop detector)."""
    if isinstance(tool_input, dict) and tool in ("Bash", "PowerShell") and isinstance(tool_input.get("command"), str):
        d = dict(tool_input)
        d["command"] = " ".join(str(d["command"]).split())
        return d
    return tool_input


def is_human_prompt(text: str) -> bool:
    """A message from a human, not harness plumbing (summaries after compaction, interruptions,
    system reminders)."""
    t = text.strip()
    if not t or t.startswith("<") or t.startswith("["):
        return False
    low = t[:200].lower()
    if low.startswith("caveat:") or "this session is being continued" in low or "summary of the conversation" in low:
        return False
    return True
