"""Normalized values produced by plugin manifest ingestion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class NormalizedSkill:
    name: str  # original skill name (from directory name / frontmatter)
    skill_content: str  # SKILL.md source text (path variables already rewritten)
    extra_files: Dict[str, str]  # {relative path: content} (text verbatim / binary base64)


@dataclass
class NormalizedMcp:
    name: str
    display_name: str
    description: str
    transport: str  # stdio | streamable_http | sse
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    url: Optional[str] = None
    env_vars: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None  # stdio working directory (Agent Plugins standard field)
    needs_runtime: bool = (
        False  # stdio → True: installed but disabled by default; enable only once the runtime is in place
    )
    note: str = ""
    tools: List[Dict[str, Any]] = field(
        default_factory=list
    )  # tool list the manifest may declare (display only, [{name,description}])


@dataclass
class NormalizedPlugin:
    slug: str
    name: str
    version: str
    description: str
    category: str
    icon: Optional[str]
    kind: str  # native | claude | codex
    required_secrets: List[Dict[str, Any]]
    default_enabled: Dict[str, List[str]]  # {"skills":[...], "mcp":[...]}
    skills: List[NormalizedSkill]
    mcp: List[NormalizedMcp]
    dropped: List[Dict[str, str]]  # [{type, name, reason}]
    # Admin-level config (provider credentials): filled in centrally by the admin
    # on the plugin detail page, stored in SystemConfig, shared by all users and
    # read-only on the user side. Shape: {"mode":"any|all", "group":..., "hint":...,
    # "fields":[{"key","label","secret","description"}]}. None = this plugin needs no admin config.
    admin_config: Optional[Dict[str, Any]] = None
    # Account connection type (per-user OAuth device flow): e.g. "dingtalk" / "lark".
    # When non-empty, the frontend renders the corresponding account-connection
    # panel on the plugin detail page where the user completes a one-time
    # authorization. None = no account connection needed.
    connection: Optional[str] = None
    # UI contributions (``extensions["org.hugagent"].ui``): which host view
    # renders which tool, canvas tabs, homepage shortcuts, proxied data sources
    # and self-shipped L2 modules. Validated by ``plugin_ui_contract``; None =
    ui: Optional[Dict[str, Any]] = None
    package_dir: Optional[str] = None


# ── Manifest detection ────────────────────────────────────────────────────────
