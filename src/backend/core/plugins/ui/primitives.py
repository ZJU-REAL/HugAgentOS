"""Shared validation rules for plugin UI declarations."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Contract version understood by this host. A manifest declaring a *higher*
# major version is ignored wholesale (the frontend falls back to generic
# rendering) rather than half-rendered from fields we may misinterpret.
SUPPORTED_UI_VERSION = 1

# ── View kinds the host's material library provides ──────────────────────────
# Grouped exactly like src/frontend/src/plugin-ui/views/: A = document-shaped,
# B = analytical, C = container / interactive. Keep this list in sync with the
# frontend registry — an unknown kind is dropped at install time so a typo shows
# up in the import report instead of as a blank card at runtime.
VIEW_KINDS_DOCUMENT = ("badge", "kv", "list", "table", "markdown", "sections", "metrics")
VIEW_KINDS_ANALYTIC = ("timeseries", "ranking", "comparison", "distribution", "score", "timeline")
VIEW_KINDS_CONTAINER = ("tree-graph", "gallery", "status-list", "trace", "link-card", "tabs")
VIEW_KINDS = VIEW_KINDS_DOCUMENT + VIEW_KINDS_ANALYTIC + VIEW_KINDS_CONTAINER

# ``tabs`` nests child views; depth is capped so a manifest cannot build an
# unbounded render tree.
MAX_TABS_DEPTH = 2

# L2 module mount points. Each reuses a container the product already has, so a
# module never invents new navigation. Grow this tuple only together with an
# actual host mount — an accepted-but-unrendered surface is a silent no-op.
MODULE_SURFACES = ("canvas", "tool_view")

# Bridge methods a module may call. ``grants`` in the manifest narrows this
# further per module; anything outside the list is refused by the host bridge.
# (Theme and locale are not grants: the bridge delivers them unconditionally.)
BRIDGE_METHODS = (
    "data.query",
    "canvas.open",
    "chat.send",
    "clipboard.write",
    "file.save",
    "host.info",
)

ACTION_TRIGGERS = ("node", "item", "primary")

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
# In-process data provider reference: ``package.module:name``. Only functions a
# host-shipped ``mcp_servers.*`` module deliberately publishes in its
# ``DATA_PROVIDERS`` dict are callable — a manifest cannot name arbitrary code.
_PROVIDER_RE = re.compile(r"^mcp_servers\.[A-Za-z0-9_.]{1,120}:[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Pointer grammar accepted by the frontend evaluator (utils/pointer.ts). It is a
# whitelist parser, not an expression engine: "$", "$.field", "$.a.b[]",
# "$.items[].name", plus the "$root." / "$node." / "$item." action scopes.
_POINTER_RE = re.compile(r"^\$(root|node|item)?(\.[^.\[\]]+(\[\])?)*$")

MAX_CONTRIBUTIONS_PER_KIND = 200
MAX_MODULE_GRANTS = 32


class _Dropper:
    """Collects per-contribution rejects so they can surface in the import report."""

    def __init__(self) -> None:
        self.dropped: List[Dict[str, str]] = []

    def drop(self, kind: str, name: str, reason: str) -> None:
        self.dropped.append({"type": f"ui.{kind}", "name": name, "reason": reason})


def _text(value: Any, *, max_len: int = 400) -> str:
    return str(value).strip()[:max_len] if isinstance(value, (str, int, float)) else ""


def _i18n_text(value: Any, *, max_len: int = 400) -> Any:
    """A display string or an i18n map ``{"zh-CN": ..., "en": ...}``.

    Decision 1 of the design doc: the contract accepts both, because plugin
    manifests are authored in Chinese today while the product already ships an
    English locale — a Chinese-only contract would render mixed-language UI.
    Resolution order is handled on the frontend: current locale → zh-CN → first
    available value.
    """
    if isinstance(value, str):
        return value.strip()[:max_len]
    if isinstance(value, dict):
        out = {
            str(k).strip()[:32]: str(v).strip()[:max_len]
            for k, v in value.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()
        }
        return out or ""
    return ""


def _pointer(value: Any) -> str:
    """One field-mapping pointer; '' when it is not valid pointer grammar."""
    text = value.strip() if isinstance(value, str) else ""
    return text if text and len(text) <= 200 and _POINTER_RE.match(text) else ""


def _mapping(value: Any, *, depth: int = 0) -> Dict[str, Any]:
    """A view's ``map`` block: pointers, literals, string lists, or nested view specs."""
    if not isinstance(value, dict) or depth > MAX_TABS_DEPTH:
        return {}
    out: Dict[str, Any] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(raw, str):
            # Pointers start with "$"; anything else is a literal (unit, kind, …).
            out[key] = _pointer(raw) or (raw.strip()[:200] if not raw.startswith("$") else "")
        elif isinstance(raw, bool):
            out[key] = raw
        elif isinstance(raw, (int, float)):
            out[key] = raw
        elif isinstance(raw, list):
            items = [
                _child_spec(x, depth=depth + 1) if isinstance(x, dict) else _text(x)
                for x in raw[:64]
            ]
            out[key] = [x for x in items if x]
        elif isinstance(raw, dict):
            child = _child_spec(raw, depth=depth + 1)
            if child:
                out[key] = child
    return {k: v for k, v in out.items() if v not in ("", [], {})}


def _child_spec(value: Any, *, depth: int) -> Dict[str, Any]:
    """A nested view spec inside ``tabs`` (or any composite map slot)."""
    if not isinstance(value, dict) or depth > MAX_TABS_DEPTH:
        return {}
    view = _text(value.get("view"), max_len=32)
    out: Dict[str, Any] = {}
    label = _i18n_text(value.get("label"))
    if label:
        out["label"] = label
    if view:
        if view not in VIEW_KINDS:
            return {}
        out["view"] = view
        out["map"] = _mapping(value.get("map"), depth=depth)
        actions = _actions(value.get("actions"))
        if actions:
            out["actions"] = actions
    elif not out:
        return {}
    return out


def _string_list(value: Any, *, pattern: Optional[re.Pattern] = None, limit: int = 64) -> List[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = _text(item, max_len=128)
        if text and (pattern is None or pattern.match(text)):
            out.append(text)
    return out


def _actions(value: Any) -> List[Dict[str, Any]]:
    """``actions[]``: click a thing → call a data source → render the result.

    Decision 3 of the design doc: this used to be ``tree-graph``'s private
    ``node_action`` field. It is promoted here because "drill down from an item"
    is something ``list`` / ``ranking`` / ``gallery`` need just as much.
    """
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in value[:16]:
        if not isinstance(raw, dict):
            continue
        action_id = _text(raw.get("id"), max_len=64)
        source = _text(raw.get("data_source"), max_len=64)
        if not (_ID_RE.match(action_id) and _ID_RE.match(source)):
            continue
        trigger = _text(raw.get("trigger"), max_len=16) or "item"
        if trigger not in ACTION_TRIGGERS:
            trigger = "item"
        params = {
            str(k)[:64]: _pointer(v) or _text(v, max_len=200)
            for k, v in (raw.get("params") or {}).items()
            if isinstance(k, str)
        }
        result_raw = raw.get("result") if isinstance(raw.get("result"), dict) else {}
        result_view = _text(result_raw.get("view"), max_len=32)
        result: Dict[str, Any] = {}
        if result_view in VIEW_KINDS:
            result = {
                "view": result_view,
                "map": _mapping(result_raw.get("map"), depth=1),
                "paged": bool(result_raw.get("paged")),
            }
            # Upstreams disagree on paging parameter names (page/pageNum,
            # page_size/pageSize/limit); the plugin names them so the host does
            # not have to guess or translate.
            if result.get("paged"):
                result["page_param"] = _text(result_raw.get("page_param"), max_len=64) or "page"
                result["page_size_param"] = (
                    _text(result_raw.get("page_size_param"), max_len=64) or "page_size"
                )
                result["page_size"] = _clamp_int(result_raw.get("page_size"), 10, 1, 100)
            # Drill-panel chrome the plugin words itself: column headers and
            # state texts (pending / loading / empty / total). Purely display —
            # the material renders its layout, the plugin supplies the words.
            columns = [
                c for c in (_i18n_text(x) for x in (result_raw.get("columns") or [])[:8]) if c
            ]
            if columns:
                result["columns"] = columns
            texts: Dict[str, Any] = {}
            if isinstance(result_raw.get("texts"), dict):
                for text_key, raw_text in list(result_raw["texts"].items())[:12]:
                    text_value = _i18n_text(raw_text)
                    if isinstance(text_key, str) and text_key and text_value:
                        texts[text_key[:32]] = text_value
            if texts:
                result["texts"] = texts
        filters = []
        for f in (raw.get("filters") or [])[:12]:
            if not isinstance(f, dict):
                continue
            key = _text(f.get("key"), max_len=64)
            if not key:
                continue
            filters.append(
                {
                    "key": key,
                    "label": _i18n_text(f.get("label")) or key,
                    "options_from": _pointer(f.get("options_from")),
                }
            )
        out.append(
            {
                "id": action_id,
                "label": _i18n_text(raw.get("label")) or action_id,
                "trigger": trigger,
                "data_source": source,
                "params": {k: v for k, v in params.items() if v},
                **({"result": result} if result else {}),
                **({"filters": filters} if filters else {}),
                **(
                    {
                        "enabled_for_tools": _string_list(
                            raw.get("enabled_for_tools"), pattern=_TOOL_RE
                        )
                    }
                    if raw.get("enabled_for_tools")
                    else {}
                ),
            }
        )
    return out


def _safe_relpath(path: str) -> str:
    """Normalize a package-relative path, rejecting absolute paths and traversal."""
    text = (path or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or ":" in text:
        return ""
    parts: List[str] = []
    for seg in text.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            return ""
        parts.append(seg)
    return "/".join(parts)


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, int(value)))


def _clamp_num(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, float(value)))


def _iter_entries(entries: Any) -> List[Dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    return [e for e in entries[:MAX_CONTRIBUTIONS_PER_KIND] if isinstance(e, dict)]
