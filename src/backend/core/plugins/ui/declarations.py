"""Parse each supported plugin UI contribution kind."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .primitives import (
    _ID_RE,
    _PROVIDER_RE,
    _TOOL_RE,
    BRIDGE_METHODS,
    MAX_MODULE_GRANTS,
    MODULE_SURFACES,
    VIEW_KINDS,
    _actions,
    _child_spec,
    _clamp_int,
    _clamp_num,
    _Dropper,
    _i18n_text,
    _iter_entries,
    _mapping,
    _pointer,
    _safe_relpath,
    _string_list,
    _text,
)


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
                    **(
                        {"icon": _text(citation.get("icon"), max_len=300)}
                        if citation.get("icon")
                        else {}
                    ),
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
            p
            for p in (
                _pointer(x)
                for x in (raw_title_from if isinstance(raw_title_from, list) else [raw_title_from])
            )
            if p
        ]
        if title_from:
            # Stored as a fallback list; the frontend's readText takes either form.
            item["title_from_input"] = title_from if len(title_from) > 1 else title_from[0]
        options = raw.get("options")
        if isinstance(options, dict):
            item["options"] = {
                str(k)[:32]: v
                for k, v in options.items()
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
        out.append(
            {
                "id": sid,
                "label": label,
                **({"icon": _text(raw.get("icon"), max_len=300)} if raw.get("icon") else {}),
                **(
                    {"prompt": _i18n_text(raw.get("prompt"), max_len=500)}
                    if raw.get("prompt")
                    else {}
                ),
            }
        )
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
            out.append(
                {
                    "id": sid,
                    "provider": provider,
                    "params_schema": _params_schema(raw.get("params_schema")),
                }
            )
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
            g
            for g in _string_list(raw.get("grants"), limit=MAX_MODULE_GRANTS)
            if g in BRIDGE_METHODS or g.startswith("data_source:")
        ]
        item: Dict[str, Any] = {
            "id": mid,
            "entry": normalized_entry,
            "surface": surface,
            "title": _i18n_text(raw.get("title")) or mid,
            "grants": grants,
        }
        if surface == "canvas" and raw.get("canvas_header") == "module":
            item["canvas_header"] = "module"
        resource = raw.get("resource")
        if resource is not None:
            import re
            root = _safe_relpath(resource.get("entry")) if isinstance(resource, dict) else ""
            callable_name = resource.get("callable", "") if isinstance(resource, dict) else ""
            if not root or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", callable_name):
                dropper.drop("modules", mid, "resource runtime 非法")
                continue
            item["resource"] = {"entry": root, "callable": callable_name}
            configuration = resource.get("configuration", {})
            if isinstance(configuration, dict):
                item["resource"]["configuration"] = configuration
            binding = _pointer(raw.get("resource_binding"))
            if binding:
                item["resource_binding"] = binding
        icon = _text(raw.get("icon"), max_len=300)
        if icon:
            item["icon"] = icon
        module_unwrap = _string_list(raw.get("unwrap"), limit=8)
        if module_unwrap:
            item["unwrap"] = module_unwrap
        for_tools = _string_list(raw.get("for_tools"), pattern=_TOOL_RE)
        if for_tools:
            item["for_tools"] = for_tools
        height = raw.get("height")
        if isinstance(height, dict):
            mode = _text(height.get("mode"), max_len=16)
            if mode in ("ratio", "fixed", "auto"):
                item["height"] = {
                    "mode": mode,
                    "value": _clamp_num(height.get("value"), 0.6, 0.1, 4000),
                }
        fallback = _child_spec(raw.get("fallback"), depth=1)
        if fallback.get("view"):
            item["fallback"] = fallback
        out.append(item)
    return out
