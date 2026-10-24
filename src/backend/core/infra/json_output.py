"""Parse structured model output with the existing fenced/prose fallbacks."""

import json
import re
from typing import Any, Dict, List, Optional


def parse_json_array_lenient(text: str) -> Optional[List[Any]]:
    if not text:
        return None
    candidates = [text.strip()]
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        candidates.append(m.group(1).strip())
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e != -1 and e > s:
        candidates.append(text[s : e + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, list):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def parse_json_lenient(text: str) -> Optional[Dict[str, Any]]:
    """Three-level lenient parsing (object), following plan_mode._parse_plan_json."""
    for candidate in _json_candidates(text):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _json_candidates(text: str):
    if not text:
        return
    yield text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        yield m.group(1).strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        yield text[s : e + 1]
