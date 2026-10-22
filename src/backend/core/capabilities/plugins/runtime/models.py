"""Progressive plugin runtime: runtime models."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Set

logger = logging.getLogger(__name__)


def progressive_plugin_loading_enabled() -> bool:
    """Env-driven kill switch (default on)."""
    return os.getenv("PLUGIN_PROGRESSIVE_LOADING", "true").strip().lower() == "true"


@dataclass
class DeferredPlugin:
    """One deferral-eligible plugin and its in-play components."""

    # Stable installation identity. ``slug`` is the user-facing selector and
    # is not globally unique (a private and global installation may share it),
    # so durable activation must use this value.
    install_id: str
    slug: str
    name: str
    description: str
    # Component ids intersected with this run's enabled sets — only what the
    # run would actually have carried is deferred / later activated.
    skill_ids: List[str] = field(default_factory=list)
    mcp_ids: List[str] = field(default_factory=list)
    # MCP servers declared by the plugin's skills via SKILL.md frontmatter
    # (mcp_server_ids). Connected at activation when not already connected —
    # NOT subtracted from the base assembly, since they may be shared with
    # non-plugin skills.
    bound_mcp_ids: List[str] = field(default_factory=list)
    # 推迟时记下的定义身份（插件与它绑定的智能体）。激活那一刻按这份身份复查：
    # 中间用户可能已经把它停用、卸载，或者定义文件被改过。
    capability_nodes: List[dict] = field(default_factory=list)
    unavailable_mcp_ids: Set[str] = field(default_factory=set)


@dataclass
class ProgressiveResolution:
    """Outcome of the assembly-time deferral decision."""

    # Plugins actually deferred this run (not activated, not invoked).
    deferred: List[DeferredPlugin] = field(default_factory=list)
    # All deferral-eligible plugins regardless of activation state, sorted by
    # slug. The directory section renders THIS list so the prompt bytes stay
    # identical before and after an activation (prefix-cache friendly).
    directory: List[DeferredPlugin] = field(default_factory=list)
    activated_slugs: List[str] = field(default_factory=list)
    deferred_skill_ids: Set[str] = field(default_factory=set)
    deferred_mcp_ids: Set[str] = field(default_factory=set)
    unavailable_skill_ids: Set[str] = field(default_factory=set)

    def deferred_by_slug(self) -> Dict[str, DeferredPlugin]:
        return {p.slug: p for p in self.deferred}


@dataclass
class StickyPluginCapabilities:
    """Authorized plugin components restored for a chat's later turns."""

    install_ids: List[str] = field(default_factory=list)
    slugs: List[str] = field(default_factory=list)
    skill_ids: List[str] = field(default_factory=list)
    mcp_ids: List[str] = field(default_factory=list)
    unavailable_ids: List[str] = field(default_factory=list)
