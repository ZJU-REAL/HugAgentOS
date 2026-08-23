"""Plugin UI contribution contract: validate / normalize ``extensions["org.hugagent"].ui``.

A plugin contributes **capabilities** (skills + MCP) through the rest of the
plugin system; this module is what lets it also contribute **interface**.  The
manifest declares *what to render* and the host owns *how to render it*, so the
frontend never needs a per-plugin branch — the inversion described in
``internal design docs``.

Three layers live in the same ``ui`` block:

- **L0 declarative** — ``tool_meta`` / ``tool_views`` / ``canvas_views`` /
  ``shortcuts``: pick one of the host's view kinds and map fields into it.
- **L1 data proxy** — ``data_sources``: a backend-proxied upstream call the
  frontend may trigger by id (credentials stay on the server).
- **L2 module** — ``modules``: frontend assets the plugin ships itself, run in a
  sandboxed iframe and talked to over the postMessage bridge.

Failure policy is **fail-soft per contribution**: an unparsable entry is dropped
with a reason (surfaced in the plugin's import report) while everything else
still installs; an unsupported ``ui.version`` drops the whole block.  A plugin
must never be uninstallable because its UI declaration has a typo.
"""

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
BRIDGE_METHODS = ("data.query", "canvas.open", "chat.send", "clipboard.write", "file.save", "host.info")

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


# ── Primitive validators ─────────────────────────────────────────────────────

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
            items = [_child_spec(x, depth=depth + 1) if isinstance(x, dict) else _text(x) for x in raw[:64]]
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


# ── Actions (shared by every view kind) ──────────────────────────────────────

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
            columns = [c for c in (
                _i18n_text(x) for x in (result_raw.get("columns") or [])[:8]
            ) if c]
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
            filters.append({
                "key": key,
                "label": _i18n_text(f.get("label")) or key,
                "options_from": _pointer(f.get("options_from")),
            })
        out.append({
            "id": action_id,
            "label": _i18n_text(raw.get("label")) or action_id,
            "trigger": trigger,
            "data_source": source,
            "params": {k: v for k, v in params.items() if v},
            **({"result": result} if result else {}),
            **({"filters": filters} if filters else {}),
            **({"enabled_for_tools": _string_list(raw.get("enabled_for_tools"), pattern=_TOOL_RE)}
               if raw.get("enabled_for_tools") else {}),
        })
    return out


# ── Contribution kinds ───────────────────────────────────────────────────────

def _tool_meta(entries: Any, dropper: _Dropper) -> List[Dict[str, Any]]:
    """Display metadata for a tool: name, icon, step text, citation type."""
    out: List[Dict[str, Any]] = []
    for raw in _iter_entries(entries):
        tool = _text(raw.get("tool"), max_len=128)
        if not _TOOL_RE.match(tool or ""):
            dropper.drop("tool_meta", tool or "?", "tool 名称非法")
            continue
        item: Dict[str, Any] = {"tool": tool}
        for key in ("label", "step_text"):
            value = _i18n_text(raw.get(key))
            if value:
                item[key] = value
        icon = _text(raw.get("icon"), max_len=300)
        if icon:
            item["icon"] = icon
        citation = raw.get("citation")
        if isinstance(citation, dict):
            ctype = _text(citation.get("type"), max_len=64)
            if ctype:
                item["citation"] = {
                    "type": ctype,
                    "label": _i18n_text(citation.get("label")) or ctype,
                    **({"icon": _text(citation.get("icon"), max_len=300)} if citation.get("icon") else {}),
                }
        if len(item) == 1:
            dropper.drop("tool_meta", tool, "没有任何可用的展示字段")
            continue
        out.append(item)
    return out


def _tool_views(entries: Any, dropper: _Dropper) -> List[Dict[str, Any]]:
    """Which view renders a tool's result, and how its fields map into it."""
    out: List[Dict[str, Any]] = []
    for raw in _iter_entries(entries):
        tools = raw.get("tool")
        tool_list = _string_list(tools if isinstance(tools, list) else [tools], pattern=_TOOL_RE)
        view = _text(raw.get("view"), max_len=32)
        label = ", ".join(tool_list) or "?"
        if not tool_list:
            dropper.drop("tool_views", label, "缺少合法的 tool 名称")
            continue
        if view not in VIEW_KINDS:
            dropper.drop("tool_views", label, f"未知 view 类型：{view or '(空)'}")
            continue
        item: Dict[str, Any] = {
            "tools": tool_list,
            "view": view,
            "map": _mapping(raw.get("map")),
        }
        unwrap = _string_list(raw.get("unwrap"), limit=8)
        if unwrap:
            item["unwrap"] = unwrap
        actions = _actions(raw.get("actions"))
        if actions:
            item["actions"] = actions
        primary = raw.get("primary_action")
        if isinstance(primary, dict):
            canvas_id = _text(primary.get("open_canvas"), max_len=64)
            if _ID_RE.match(canvas_id or ""):
                entry: Dict[str, Any] = {"open_canvas": canvas_id}
                # 卡片文案由插件自己措辞：标题 / 副标题。
                for key in ("label", "sublabel"):
                    value = _i18n_text(primary.get(key))
                    if value:
                        entry[key] = value
                item["primary_action"] = entry
        out.append(item)
    return out


def _canvas_views(entries: Any, dropper: _Dropper) -> List[Dict[str, Any]]:
    """Right-side canvas tabs (the industry-chain graph is one of these)."""
    out: List[Dict[str, Any]] = []
    for raw in _iter_entries(entries):
        view_id = _text(raw.get("id"), max_len=64)
        view = _text(raw.get("view"), max_len=32)
        if not _ID_RE.match(view_id or ""):
            dropper.drop("canvas_views", view_id or "?", "id 非法")
            continue
        if view not in VIEW_KINDS:
            dropper.drop("canvas_views", view_id, f"未知 view 类型：{view or '(空)'}")
            continue
        item: Dict[str, Any] = {
            "id": view_id,
            "view": view,
            "title": _i18n_text(raw.get("title")) or view_id,
            "map": _mapping(raw.get("map")),
        }
        # Same envelope-peeling as tool_views: a canvas is fed the raw tool
        # output, which upstreams routinely wrap in result/结果 layers.
        unwrap = _string_list(raw.get("unwrap"), limit=8)
        if unwrap:
            item["unwrap"] = unwrap
        icon = _text(raw.get("icon"), max_len=300)
        if icon:
            item["icon"] = icon
        auto = _string_list(raw.get("auto_open_on_tools"), pattern=_TOOL_RE)
        if auto:
            item["auto_open_on_tools"] = auto
        raw_title_from = raw.get("title_from_input")
        title_from = [
            p for p in (
                _pointer(x) for x in (
                    raw_title_from if isinstance(raw_title_from, list) else [raw_title_from]
                )
            ) if p
        ]
        if title_from:
            # Stored as a fallback list; the frontend's readText takes either form.
            item["title_from_input"] = title_from if len(title_from) > 1 else title_from[0]
        options = raw.get("options")
        if isinstance(options, dict):
            item["options"] = {
                str(k)[:32]: v for k, v in options.items()
                if isinstance(k, str) and isinstance(v, (str, int, float, bool))
            }
        actions = _actions(raw.get("actions"))
        if actions:
            item["actions"] = actions
        out.append(item)
    return out


def _shortcuts(entries: Any, dropper: _Dropper) -> List[Dict[str, Any]]:
    """Homepage quick-entry chips contributed by the plugin."""
    out: List[Dict[str, Any]] = []
    for raw in _iter_entries(entries):
        sid = _text(raw.get("id"), max_len=64)
        if not _ID_RE.match(sid or ""):
            dropper.drop("shortcuts", sid or "?", "id 非法")
            continue
        label = _i18n_text(raw.get("label"))
        if not label:
            dropper.drop("shortcuts", sid, "缺少 label")
            continue
        out.append({
            "id": sid,
            "label": label,
            **({"icon": _text(raw.get("icon"), max_len=300)} if raw.get("icon") else {}),
            **({"prompt": _i18n_text(raw.get("prompt"), max_len=500)} if raw.get("prompt") else {}),
        })
    return out


def _data_sources(entries: Any, dropper: _Dropper) -> List[Dict[str, Any]]:
    """L1 proxied upstream calls.

    Two shapes, mutually exclusive:

    - ``url`` (+ optional ``auth``): the proxy performs the HTTP call itself.
    - ``provider``: an in-process handler published by a host-shipped
      ``mcp_servers.*`` module in its ``DATA_PROVIDERS`` dict — for upstreams
      that need envelope-stripping / normalization no declarative call can do.

    ``url`` / ``auth`` / ``provider`` are stored but **never** shipped to the
    browser — see ``public_contributions``. The frontend only ever names a
    source by id.
    """
    out: List[Dict[str, Any]] = []
    for raw in _iter_entries(entries):
        sid = _text(raw.get("id"), max_len=64)
        url = _text(raw.get("url"), max_len=800)
        provider = _text(raw.get("provider"), max_len=200)
        if not _ID_RE.match(sid or ""):
            dropper.drop("data_sources", sid or "?", "id 非法")
            continue
        if provider:
            if not _PROVIDER_RE.match(provider):
                dropper.drop("data_sources", sid, "provider 引用非法")
                continue
            out.append({
                "id": sid,
                "provider": provider,
                "params_schema": _params_schema(raw.get("params_schema")),
            })
            continue
        if not url:
            dropper.drop("data_sources", sid, "缺少 url 或 provider")
            continue
        method = (_text(raw.get("method"), max_len=8) or "POST").upper()
        if method not in ("GET", "POST"):
            dropper.drop("data_sources", sid, f"不支持的 method：{method}")
            continue
        auth = raw.get("auth") if isinstance(raw.get("auth"), dict) else {}
        item: Dict[str, Any] = {
            "id": sid,
            "method": method,
            "url": url,
            "timeout_ms": _clamp_int(raw.get("timeout_ms"), 10_000, 1_000, 60_000),
            "max_bytes": _clamp_int(raw.get("max_bytes"), 1_048_576, 1_024, 8_388_608),
            "params_schema": _params_schema(raw.get("params_schema")),
        }
        header = _text(auth.get("header"), max_len=64)
        if header:
            item["auth"] = {"header": header, "value": _text(auth.get("value"), max_len=500)}
        out.append(item)
    return out


def _params_schema(value: Any) -> Dict[str, Any]:
    """Per-parameter validation rules, enforced server-side on every proxy call."""
    if not isinstance(value, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, raw in list(value.items())[:32]:
        if not isinstance(key, str) or not isinstance(raw, dict):
            continue
        ptype = _text(raw.get("type"), max_len=16) or "string"
        if ptype not in ("string", "integer", "number", "boolean", "array"):
            continue
        spec: Dict[str, Any] = {"type": ptype, "required": bool(raw.get("required"))}
        for bound in ("min", "max", "max_length"):
            if isinstance(raw.get(bound), (int, float)) and not isinstance(raw.get(bound), bool):
                spec[bound] = raw[bound]
        if "default" in raw and isinstance(raw["default"], (str, int, float, bool, list)):
            spec["default"] = raw["default"]
        out[key[:64]] = spec
    return out


def _modules(entries: Any, dropper: _Dropper) -> List[Dict[str, Any]]:
    """L2 modules: frontend assets shipped **inside the plugin package**.

    ``entry`` is a package-relative path under the plugin's own ``web/``
    directory — plugin UI code never lands in the host frontend tree.
    """
    out: List[Dict[str, Any]] = []
    for raw in _iter_entries(entries):
        mid = _text(raw.get("id"), max_len=64)
        entry = _text(raw.get("entry"), max_len=300)
        if not _ID_RE.match(mid or ""):
            dropper.drop("modules", mid or "?", "id 非法")
            continue
        normalized_entry = _safe_relpath(entry)
        if not normalized_entry:
            dropper.drop("modules", mid, "entry 路径非法（必须是包内相对路径，且不能越级）")
            continue
        if not normalized_entry.startswith("web/"):
            dropper.drop("modules", mid, "entry 必须位于插件包的 web/ 目录下")
            continue
        surface = _text(raw.get("surface"), max_len=16) or "canvas"
        if surface not in MODULE_SURFACES:
            dropper.drop("modules", mid, f"未知 surface：{surface}")
            continue
        grants = [
            g for g in _string_list(raw.get("grants"), limit=MAX_MODULE_GRANTS)
            if g in BRIDGE_METHODS or g.startswith("data_source:")
        ]
        item: Dict[str, Any] = {
            "id": mid,
            "entry": normalized_entry,
            "surface": surface,
            "title": _i18n_text(raw.get("title")) or mid,
            "grants": grants,
        }
        icon = _text(raw.get("icon"), max_len=300)
        if icon:
            item["icon"] = icon
        for_tools = _string_list(raw.get("for_tools"), pattern=_TOOL_RE)
        if for_tools:
            item["for_tools"] = for_tools
        height = raw.get("height")
        if isinstance(height, dict):
            mode = _text(height.get("mode"), max_len=16)
            if mode in ("ratio", "fixed", "auto"):
                item["height"] = {"mode": mode, "value": _clamp_num(height.get("value"), 0.6, 0.1, 4000)}
        fallback = _child_spec(raw.get("fallback"), depth=1)
        if fallback.get("view"):
            item["fallback"] = fallback
        out.append(item)
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


# ── Entry points ─────────────────────────────────────────────────────────────

_KIND_PARSERS = {
    "tool_meta": _tool_meta,
    "tool_views": _tool_views,
    "canvas_views": _canvas_views,
    "shortcuts": _shortcuts,
    "data_sources": _data_sources,
    "modules": _modules,
}


def normalize_ui(manifest_ui: Any) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, str]]]:
    """Validate a manifest's ``ui`` block.

    Returns ``(contributions, dropped)``; ``contributions`` is None when the
    block is absent or its version is unsupported (the plugin then simply has no
    UI contributions and every tool falls back to generic rendering).
    """
    if not isinstance(manifest_ui, dict):
        return None, []
    dropper = _Dropper()
    version = manifest_ui.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        dropper.drop("ui", "version", "ui.version 必须是 >= 1 的整数")
        return None, dropper.dropped
    if version > SUPPORTED_UI_VERSION:
        dropper.drop(
            "ui", f"version={version}",
            f"契约版本高于当前平台支持的 {SUPPORTED_UI_VERSION}，已整块忽略（请升级 HugAgentOS）",
        )
        return None, dropper.dropped

    raw_contributes = manifest_ui.get("contributes")
    if not isinstance(raw_contributes, dict):
        dropper.drop("ui", "contributes", "缺少 contributes 对象")
        return None, dropper.dropped

    contributes: Dict[str, Any] = {}
    for kind, parser in _KIND_PARSERS.items():
        parsed = parser(raw_contributes.get(kind), dropper)
        if parsed:
            contributes[kind] = parsed

    unknown = [k for k in raw_contributes if k not in _KIND_PARSERS]
    for key in unknown[:16]:
        dropper.drop("ui", str(key)[:64], "未知的贡献点类型，已忽略")

    if not contributes:
        return None, dropper.dropped
    return {"version": version, "contributes": contributes}, dropper.dropped


def public_contributions(stored: Any, *, slug: str) -> Dict[str, Any]:
    """Browser-facing projection of one plugin's contributions.

    Strips everything the frontend must not see: a data source's upstream
    ``url`` and its ``auth`` header (which carries the interpolated admin
    credential). The browser only learns that a source *id* exists and what
    parameters it accepts.
    """
    if not isinstance(stored, dict):
        return {}
    contributes = stored.get("contributes")
    if not isinstance(contributes, dict):
        return {}
    out: Dict[str, Any] = {}
    for kind, entries in contributes.items():
        if kind not in _KIND_PARSERS or not isinstance(entries, list):
            continue
        if kind == "data_sources":
            out[kind] = [
                {
                    "id": e.get("id"),
                    # provider-backed sources have no declared method; the
                    # proxy endpoint is POST-only from the browser's side.
                    "method": e.get("method") or "POST",
                    "params_schema": e.get("params_schema") or {},
                }
                for e in entries
                if isinstance(e, dict) and e.get("id")
            ]
        else:
            out[kind] = entries
    return {"slug": slug, "version": stored.get("version", SUPPORTED_UI_VERSION), "contributes": out}


def find_data_source(stored: Any, source_id: str) -> Optional[Dict[str, Any]]:
    """The full (server-side) definition of one data source, or None."""
    if not isinstance(stored, dict):
        return None
    for entry in ((stored.get("contributes") or {}).get("data_sources") or []):
        if isinstance(entry, dict) and entry.get("id") == source_id:
            return entry
    return None


def find_module(stored: Any, module_id: str) -> Optional[Dict[str, Any]]:
    """The definition of one L2 module, or None."""
    if not isinstance(stored, dict):
        return None
    for entry in ((stored.get("contributes") or {}).get("modules") or []):
        if isinstance(entry, dict) and entry.get("id") == module_id:
            return entry
    return None
