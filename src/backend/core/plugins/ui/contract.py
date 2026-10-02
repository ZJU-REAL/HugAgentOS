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

from typing import Any, Dict, List, Optional, Tuple

from .declarations import (
    _canvas_views,
    _data_sources,
    _modules,
    _shortcuts,
    _tool_meta,
    _tool_views,
)
from .primitives import SUPPORTED_UI_VERSION, _Dropper

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
            "ui",
            f"version={version}",
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
    return {
        "slug": slug,
        "version": stored.get("version", SUPPORTED_UI_VERSION),
        "contributes": out,
    }


def find_data_source(stored: Any, source_id: str) -> Optional[Dict[str, Any]]:
    """The full (server-side) definition of one data source, or None."""
    if not isinstance(stored, dict):
        return None
    for entry in (stored.get("contributes") or {}).get("data_sources") or []:
        if isinstance(entry, dict) and entry.get("id") == source_id:
            return entry
    return None


def find_module(stored: Any, module_id: str) -> Optional[Dict[str, Any]]:
    """The definition of one L2 module, or None."""
    if not isinstance(stored, dict):
        return None
    for entry in (stored.get("contributes") or {}).get("modules") or []:
        if isinstance(entry, dict) and entry.get("id") == module_id:
            return entry
    return None
