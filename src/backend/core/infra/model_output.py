"""Shared reasoning-block cleanup and tolerant JSON extraction for model output."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)


_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    """Strip a reasoning model's think blocks and return the body.

    Covers three shapes:
    - closed ``<think>...</think>`` blocks (possibly multiple);
    - only ``</think>`` (some serving stacks swallow the opening tag into the chat template, with the body following the closing tag)
      → take the content after the last closing tag;
    - only ``<think>`` (output was truncated mid-thought by max_tokens, with no body after)
      → discard everything from the opening tag onward.
    """
    t = THINK_BLOCK_RE.sub("", text)
    m = None
    for m in _THINK_CLOSE_RE.finditer(t):
        pass
    if m is not None:
        t = t[m.end():]
    m = _THINK_OPEN_RE.search(t)
    if m is not None:
        t = t[: m.start()]
    return t.strip()


def extract_first_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start=start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_strict_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse JSON tolerantly: strips <think> blocks, ``` fences, and surrounding prose."""
    if not text:
        return None
    # If the body is already valid JSON, return it directly to avoid corruption from stripping a think tag that happens to appear inside a string value
    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        parsed = None
    t = strip_think_blocks(text.strip())

    if t.startswith("```"):
        lines = [l for l in t.splitlines() if not l.strip().startswith("```")]
        t = "\n".join(lines).strip()

    try:
        return json.loads(t)
    except json.JSONDecodeError:
        parsed = None

    candidate = extract_first_json_object(t)
    if candidate:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None
    return None
