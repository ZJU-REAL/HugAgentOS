"""Edition-neutral business logic for personal and administrator sub-agents."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from core.db.models import UserAgent
from core.db.repository import AuditLogRepository, UserAgentRepository
from core.ontology.build_validator import ensure_ontology_build_valid
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_USER_AGENTS = 20
DEFAULT_AGENT_VERSION = "V1.0"
MAX_CHANGE_HISTORY = 30
NON_VERSIONED_FIELDS = {"is_enabled"}
VERSIONED_FIELDS = {
    "name": "名称",
    "description": "简介",
    "system_prompt": "角色设定",
    "welcome_message": "开场白",
    "suggested_questions": "推荐问题",
    "mcp_server_ids": "绑定工具",
    "skill_ids": "绑定技能",
    "plugin_ids": "绑定插件",
    "kb_ids": "绑定知识库",
    "model_provider_id": "模型",
    "temperature": "温度",
    "max_tokens": "最大输出长度",
    "max_iters": "最大推理轮次",
    "timeout": "超时时间",
    "is_enabled": "启用状态",
}


class UserAgentBaseService:
    """Service for user agent CRUD and permission checks."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = UserAgentRepository(db)

    # ── Queries ──────────────────────────────────────────────────────

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        agents = self.repo.list_for_user(user_id)
        return [self._serialize(a) for a in agents]

    def list_admin(self) -> List[Dict[str, Any]]:
        agents = self.repo.list_admin()
        return [self._serialize(a) for a in agents]

    def get_by_id(self, agent_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        agent = self.repo.get_by_id(agent_id)
        if not agent:
            raise LookupError(f"Agent {agent_id} not found")
        if user_id and not self._is_accessible(agent, user_id):
            raise PermissionError("No access to this agent")
        return self._serialize(agent)

    def get_raw_by_id(self, agent_id: str, user_id: Optional[str] = None) -> UserAgent:
        """Return the ORM object (for direct use by workflow/factory)."""
        agent = self.repo.get_by_id(agent_id)
        if not agent:
            raise LookupError(f"Agent {agent_id} not found")
        if user_id and not self._is_accessible(agent, user_id):
            raise PermissionError("No access to this agent")
        return agent

    # ── Mutations ────────────────────────────────────────────────────

    def create(
        self,
        user_id: Optional[str],
        operator_name: Optional[str],
        owner_type: str,
        data: Dict[str, Any],
        scope_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = dict(data)
        incoming_extra = dict(data.get("extra_config") or {})
        ontology_tags = list(
            data.pop("ontology_tags", incoming_extra.get("ontology_tags") or []) or []
        )
        incoming_extra["ontology_tags"] = ontology_tags
        data["extra_config"] = incoming_extra
        ensure_ontology_build_valid(
            self.db,
            asset_type="subagent",
            name=str(data.get("name") or ""),
            description=str(data.get("description") or ""),
            instructions=str(data.get("system_prompt") or ""),
            mcp_server_ids=list(data.get("mcp_server_ids") or []),
            skill_ids=list(data.get("skill_ids") or []),
            plugin_ids=list(data.get("plugin_ids") or []),
            ontology_tags=ontology_tags,
        )
        self._validate_create_scope(user_id, owner_type, scope_id)

        agent_id = f"ua_{uuid.uuid4().hex[:16]}"
        created_at = self._now_iso()
        creation_history = [
            {
                "version": DEFAULT_AGENT_VERSION,
                "timestamp": created_at,
                "content": "创建了子智能体",
                "operator_name": operator_name or user_id or "未知用户",
                "details": [],
            }
        ]
        extra_config = self._merge_extra_config(
            current_extra=None,
            incoming_extra=incoming_extra,
            version=DEFAULT_AGENT_VERSION,
            change_history=creation_history,
        )

        record = {
            "agent_id": agent_id,
            "owner_type": owner_type,
            **self._owner_fields(user_id, owner_type, scope_id),
            "created_by": user_id,
            **data,
            "extra_config": extra_config,
        }
        agent = self.repo.create(record)
        self._audit(
            user_id,
            "agent.create",
            agent_id,
            {"owner_type": owner_type, "name": data.get("name")},
        )
        return self._serialize(agent)

    def update(
        self,
        agent_id: str,
        user_id: Optional[str],
        operator_name: Optional[str],
        owner_type: str,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        agent = self.repo.get_by_id(agent_id)
        if not agent:
            raise LookupError(f"Agent {agent_id} not found")
        self._check_ownership(agent, user_id, owner_type)

        data = dict(data)
        current_extra = dict(agent.extra_config or {})
        incoming_extra = dict(data.get("extra_config") or {})
        ontology_tags = list(
            data.pop(
                "ontology_tags",
                incoming_extra.get("ontology_tags", current_extra.get("ontology_tags") or []),
            )
            or []
        )
        incoming_extra["ontology_tags"] = ontology_tags
        if "extra_config" in data or ontology_tags != list(
            current_extra.get("ontology_tags") or []
        ):
            data["extra_config"] = incoming_extra
        ensure_ontology_build_valid(
            self.db,
            asset_type="subagent",
            name=str(data.get("name", agent.name) or ""),
            description=str(data.get("description", agent.description) or ""),
            instructions=str(data.get("system_prompt", agent.system_prompt) or ""),
            mcp_server_ids=list(data.get("mcp_server_ids", agent.mcp_server_ids) or []),
            skill_ids=list(data.get("skill_ids", agent.skill_ids) or []),
            plugin_ids=list(data.get("plugin_ids", agent.plugin_ids) or []),
            ontology_tags=ontology_tags,
        )

        changed_fields = self._collect_changed_fields(agent, data)
        versioned_fields = [field for field in changed_fields if field not in NON_VERSIONED_FIELDS]
        changed_labels = [VERSIONED_FIELDS[field] for field in changed_fields]
        next_version = self._read_version(current_extra)
        change_history = self._read_change_history(current_extra)

        if changed_labels:
            change_summary = self._build_change_summary(changed_fields, data)
            change_details = self._build_change_details(agent, changed_fields, data)
            entry_version = next_version
            if versioned_fields:
                next_version = self._increment_version(next_version)
                entry_version = next_version
            change_history.append(
                {
                    "version": entry_version,
                    "timestamp": self._now_iso(),
                    "content": change_summary,
                    "operator_name": operator_name or user_id or "未知用户",
                    "details": change_details,
                }
            )
            change_history = change_history[-MAX_CHANGE_HISTORY:]

        payload = dict(data)
        payload["extra_config"] = self._merge_extra_config(
            current_extra=current_extra,
            incoming_extra=incoming_extra,
            version=next_version,
            change_history=change_history,
        )

        agent = self.repo.update(agent_id, payload)
        audit_details = {"fields": list(data.keys())}
        if changed_labels:
            audit_details["change_summary"] = change_summary
            audit_details["version"] = next_version
        self._audit(user_id, "agent.update", agent_id, audit_details)
        return self._serialize(agent)

    def delete(
        self,
        agent_id: str,
        user_id: Optional[str],
        owner_type: str,
    ) -> bool:
        agent = self.repo.get_by_id(agent_id)
        if not agent:
            raise LookupError(f"Agent {agent_id} not found")
        self._check_ownership(agent, user_id, owner_type)

        ok = self.repo.delete(agent_id)
        self._audit(user_id, "agent.delete", agent_id)
        return ok

    def toggle_enabled(self, agent_id: str) -> Dict[str, Any]:
        agent = self.repo.get_by_id(agent_id)
        if not agent:
            raise LookupError(f"Agent {agent_id} not found")
        new_val = not agent.is_enabled
        agent = self.repo.update(agent_id, {"is_enabled": new_val})
        return self._serialize(agent)

    # ── Available resources ──────────────────────────────────────────

    def list_available_resources(self, owner_user_id: Optional[str] = None) -> Dict[str, Any]:
        """Return MCP servers, skills, plugins, and KB spaces bindable to agents.

        Plugin-sourced skills/MCP are removed from the skills/mcp_servers lists
        and instead bound as a whole via the ``plugins`` list (plugin =
        installable/removable unit, expanded at runtime into its skills+tools).
        owner_user_id is used to include that user's private plugins and MCPs.

        The MCP list intentionally contains capabilities the user has personally
        switched off.  A sub-agent binding is an explicit, narrower capability
        grant and therefore may opt into one of those tools without turning it
        on for the user's main agent.  Deployment-global MCPs disabled by an
        administrator remain unavailable.
        """
        from core.db.models import AdminMcpServer, InstalledPlugin, KBSpace
        from sqlalchemy import or_

        # ── Plugin list + their component id sets (used to strip plugin capabilities from the loose skills/tools) ──
        # Query the ORM directly for component_ids (authoritative): global plugins + current user's private plugins.
        try:
            pq = self.db.query(InstalledPlugin)
            if owner_user_id:
                pq = pq.filter(
                    or_(
                        InstalledPlugin.owner_user_id == owner_user_id,
                        InstalledPlugin.owner_user_id.is_(None),
                    )
                )
            else:
                pq = pq.filter(InstalledPlugin.owner_user_id.is_(None))
            plugin_rows = pq.order_by(InstalledPlugin.created_at.desc()).all()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to list installed plugins: %s", exc)
            plugin_rows = []

        plugin_skill_ids: set = set()
        plugin_mcp_ids: set = set()
        # The same plugin may exist both as a global version (install_id=slug@global)
        # and as the user's private version (slug@<uid>) — same name → duplicate
        # display (the user sees two "定时任务管理" entries). Dedupe by slug and
        # show only one: prefer the user's private version (its components carry
        # the user's fingerprint and match their sandbox credentials); fall back
        # to the global version when there is no private one.
        # Note: plugin_skill_ids / plugin_mcp_ids still accumulate from **all**
        # rows (including the deduped-away one), so the loose skill/MCP lists
        # can fully exclude both versions' components.
        _plugin_by_slug: Dict[str, Dict[str, Any]] = {}
        for p in plugin_rows:
            cids = p.component_ids or {}
            s_ids = list(cids.get("skills") or [])
            m_ids = list(cids.get("mcp") or [])
            plugin_skill_ids.update(s_ids)
            plugin_mcp_ids.update(m_ids)
            slug = (p.install_id or "").rsplit("@", 1)[0]
            is_owned = bool(owner_user_id) and p.owner_user_id == owner_user_id
            existing = _plugin_by_slug.get(slug)
            if existing is None or (is_owned and not existing["_owned"]):
                _plugin_by_slug[slug] = {
                    "id": p.install_id,
                    "name": p.name,
                    "description": p.description or "",
                    "skill_count": len(s_ids),
                    "mcp_count": len(m_ids),
                    "_owned": is_owned,
                }
        plugin_list: List[Dict[str, Any]] = [
            {k: v for k, v in item.items() if k != "_owned"} for item in _plugin_by_slug.values()
        ]

        # Built-in plugin MCPs can also be present in the static catalog without
        # a source_plugin DB row.  They still belong under the plugin selector,
        # not the loose MCP selector.
        try:
            from core.services.plugin_service import builtin_plugin_component_ids

            _, builtin_plugin_mcp_ids = builtin_plugin_component_ids()
            plugin_mcp_ids.update(builtin_plugin_mcp_ids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to load built-in plugin MCP ids: %s", exc)

        # Resolve the user's personal on/off layer once so each MCP option can
        # explain whether it is already enabled for the main agent.  This flag
        # is display metadata only; disabled options remain bindable here.
        enabled_mcp_ids: Optional[set[str]] = None
        if owner_user_id:
            try:
                from core.config.catalog_resolver import resolve_all_runtime_enabled

                _skills, _agents, resolved_mcps = resolve_all_runtime_enabled(
                    self.db, owner_user_id
                )
                if resolved_mcps is not None:
                    enabled_mcp_ids = set(resolved_mcps)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to resolve user MCP enablement: %s", exc)

        # ── MCP tools (exclude plugin-sourced + owner isolation) ──
        # Owner isolation: private entries (owner_user_id non-null) are visible
        # only to their owner. Otherwise private copies produced by other users
        # installing the same plugin (e.g. automation-automation_task-<their
        # fingerprint>) would all leak into this user's bindable list →
        # duplicates of "定时任务" etc. Empty owner = globally shared entry,
        # visible to everyone.
        _mcp_owner_ok = (
            or_(
                AdminMcpServer.owner_user_id.is_(None),
                AdminMcpServer.owner_user_id == owner_user_id,
            )
            if owner_user_id
            else AdminMcpServer.owner_user_id.is_(None)
        )
        mcp_servers = (
            self.db.query(AdminMcpServer)
            .filter(
                _mcp_owner_ok,
                # Authoritative exclusion: any plugin-sourced MCP (source_plugin
                # nonnull) never enters the loose list — it is bound as a whole via
                # the plugins list instead. More robust than checking only
                # plugin_mcp_ids (doesn't depend on the plugin row still
                # existing/being visible), and also blocks orphaned plugin MCP rows.
                AdminMcpServer.source_plugin.is_(None),
            )
            .order_by(AdminMcpServer.sort_order)
            .all()
        )
        mcp_list: List[Dict[str, Any]] = []
        seen_mcp_ids: set = set()
        for server in mcp_servers:
            # A global false is an administrator lock.  A private false is the
            # owner's personal off state and remains eligible for an explicit
            # sub-agent binding.
            if server.owner_user_id is None and not server.is_enabled:
                continue
            if server.server_id in plugin_mcp_ids:
                continue
            mcp_list.append(
                {
                    "id": server.server_id,
                    "name": server.display_name,
                    "description": server.description,
                    "enabled": (
                        server.server_id in enabled_mcp_ids
                        if enabled_mcp_ids is not None
                        else bool(server.is_enabled)
                    ),
                }
            )
            seen_mcp_ids.add(server.server_id)

        # Some umbrella/built-in MCP entries are catalog-defined and do not
        # necessarily have a same-named AdminMcpServer row.  Include every
        # administrator-enabled catalog item so the selector is complete.
        try:
            from core.config.catalog_runtime import get_runtime_catalog

            runtime_catalog = get_runtime_catalog(self.db, include_runtime_details=False)
            for item in runtime_catalog.get("mcp") or []:
                item_id = str(item.get("id") or "").strip()
                if (
                    not item_id
                    or item_id in seen_mcp_ids
                    or item_id in plugin_mcp_ids
                    or not bool(item.get("enabled", True))
                ):
                    continue
                mcp_list.append(
                    {
                        "id": item_id,
                        "name": item.get("name") or item_id,
                        "description": item.get("description") or item.get("desc") or "",
                        "enabled": (
                            item_id in enabled_mcp_ids
                            if enabled_mcp_ids is not None
                            else bool(item.get("enabled", True))
                        ),
                    }
                )
                seen_mcp_ids.add(item_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to load catalog MCP resources: %s", exc)

        # ── Skills: DB-managed + filesystem-discovered (both exclude plugin-sourced + owner isolation) ──
        # ⚠️ Critical: the filesystem loader (load_all_metadata) scans **all**
        # materialized skills on disk — including other users' private skills
        # and plugin skills materialized by other users' plugin installs.
        # Excluding via plugin_skill_ids alone (components of plugins visible to
        # the current user only) would miss some, letting other users' plugin
        # skills sneak into "bindable skills". So compute two **authoritative**
        # exclusion sets directly from AdminSkill and filter both sources
        # uniformly:
        #   - all_plugin_skill_ids: any plugin-sourced skill (source_plugin non-null, any owner);
        #   - foreign_private_skill_ids: other users' private skills (owner_user_id non-null and ≠ current user).
        from core.db.models import AdminSkill

        all_plugin_skill_ids: set = set()
        foreign_private_skill_ids: set = set()
        try:
            for row in self.db.query(
                AdminSkill.skill_id, AdminSkill.source_plugin, AdminSkill.owner_user_id
            ).all():
                if row.source_plugin:
                    all_plugin_skill_ids.add(row.skill_id)
                if row.owner_user_id and row.owner_user_id != owner_user_id:
                    foreign_private_skill_ids.add(row.skill_id)
        except Exception as exc:
            logger.debug("Failed to precompute skill exclusion sets: %s", exc)

        _excluded_skill_ids = plugin_skill_ids | all_plugin_skill_ids | foreign_private_skill_ids

        skill_list: List[Dict[str, Any]] = []
        try:
            _skill_owner_ok = (
                or_(AdminSkill.owner_user_id.is_(None), AdminSkill.owner_user_id == owner_user_id)
                if owner_user_id
                else AdminSkill.owner_user_id.is_(None)
            )
            db_skills = (
                self.db.query(AdminSkill)
                .filter(
                    AdminSkill.is_enabled == True,
                    _skill_owner_ok,
                )
                .order_by(AdminSkill.updated_at.desc())
                .all()
            )
            seen_ids = set()
            for s in db_skills:
                if s.skill_id in _excluded_skill_ids:
                    continue
                skill_list.append(
                    {"id": s.skill_id, "name": s.display_name, "description": s.description or ""}
                )
                seen_ids.add(s.skill_id)
        except Exception:
            seen_ids = set()

        try:
            from core.agent_skills.loader import get_skill_loader

            loader = get_skill_loader()
            for sid, meta in loader.load_all_metadata().items():
                if sid not in seen_ids and sid not in _excluded_skill_ids:
                    skill_list.append(
                        {
                            "id": sid,
                            "name": getattr(meta, "name", sid),
                            "description": getattr(meta, "description", ""),
                        }
                    )
        except Exception as exc:
            logger.debug("Failed to load filesystem skills: %s", exc)

        # KB spaces
        kb_list: List[Dict[str, Any]] = []
        try:
            kb_spaces = (
                self.db.query(KBSpace)
                .filter(
                    KBSpace.deleted_at.is_(None),
                )
                .order_by(KBSpace.created_at.desc())
                .all()
            )
            kb_list = [
                {"id": s.kb_id, "name": s.name, "description": s.description or ""}
                for s in kb_spaces
            ]
        except Exception:
            pass

        return {
            "mcp_servers": mcp_list,
            "skills": skill_list,
            "plugins": plugin_list,
            "kb_spaces": kb_list,
            "ontology_tags": self._ontology_tag_options(),
        }

    def _ontology_tag_options(self) -> List[Dict[str, Any]]:
        """Controlled labels that activate a sub-agent ontology workflow at runtime."""
        from core.services.ontology_service import OntologyService

        return OntologyService(self.db).list_asset_tag_options("subagent")

    # ── Helpers ───────────────────────────────────────────────────────

    def _validate_create_scope(
        self, user_id: Optional[str], owner_type: str, scope_id: Optional[str]
    ) -> None:
        if owner_type == "user":
            if not user_id:
                raise ValueError("user_id required for user agents")
            if self.repo.count_user_agents(user_id) >= MAX_USER_AGENTS:
                raise ValueError(f"Maximum {MAX_USER_AGENTS} agents per user reached")
            return
        if owner_type == "admin":
            return
        raise ValueError(f"Unsupported agent owner type: {owner_type}")

    @staticmethod
    def _owner_fields(
        user_id: Optional[str], owner_type: str, scope_id: Optional[str]
    ) -> Dict[str, Any]:
        return {"user_id": user_id if owner_type == "user" else None}

    def _is_accessible(self, agent: UserAgent, user_id: str) -> bool:
        if agent.owner_type == "admin" and agent.is_enabled:
            return True
        if agent.owner_type == "user" and agent.user_id == user_id:
            return True
        return False

    def _check_ownership(self, agent: UserAgent, user_id: Optional[str], owner_type: str) -> None:
        # owner_type is the route context: 'admin' = Admin console routes, everything else = user-side routes
        if owner_type == "admin":
            if agent.owner_type != "admin":
                raise PermissionError("Admin can only modify admin agents")
            return
        if agent.owner_type == "user" and agent.user_id == user_id:
            return
        raise PermissionError("You can only modify your own agents")

    @classmethod
    def _serialize(cls, agent: UserAgent) -> Dict[str, Any]:
        extra_config = agent.extra_config or {}
        return {
            "agent_id": agent.agent_id,
            "owner_type": agent.owner_type,
            "user_id": agent.user_id,
            **cls._serialize_scope(agent),
            "name": agent.name,
            "avatar": agent.avatar,
            "description": agent.description,
            "system_prompt": agent.system_prompt,
            "welcome_message": agent.welcome_message,
            "suggested_questions": agent.suggested_questions or [],
            "mcp_server_ids": agent.mcp_server_ids or [],
            "skill_ids": agent.skill_ids or [],
            "plugin_ids": agent.plugin_ids or [],
            "kb_ids": agent.kb_ids or [],
            "model_provider_id": agent.model_provider_id,
            "temperature": float(agent.temperature) if agent.temperature is not None else None,
            "max_tokens": agent.max_tokens,
            "max_iters": agent.max_iters,
            "timeout": agent.timeout,
            "is_enabled": agent.is_enabled,
            "sort_order": agent.sort_order,
            "source_market_slug": agent.source_market_slug,
            "ontology_tags": list(extra_config.get("ontology_tags") or []),
            "extra_config": extra_config,
            "version": cls._read_version(extra_config),
            "change_history": cls._read_change_history(extra_config),
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
            "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
            "created_by": agent.created_by,
        }

    @staticmethod
    def _serialize_scope(agent: UserAgent) -> Dict[str, Any]:
        return {}

    def _audit(
        self, user_id: Optional[str], action: str, resource_id: str, details: Dict = None
    ) -> None:
        try:
            audit_repo = AuditLogRepository(self.db)
            audit_repo.create(
                {
                    "user_id": user_id,
                    "action": action,
                    "resource_type": "user_agent",
                    "resource_id": resource_id,
                    "details": details or {},
                    "status": "success",
                }
            )
        except Exception as exc:
            logger.warning("Audit log failed: %s", exc)

    @staticmethod
    def _normalize_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, list):
            return list(value)
        return value

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().replace(microsecond=0).isoformat()

    @classmethod
    def _collect_changed_fields(cls, agent: UserAgent, data: Dict[str, Any]) -> List[str]:
        fields: List[str] = []
        for field in VERSIONED_FIELDS:
            if field not in data:
                continue
            old_value = cls._normalize_value(getattr(agent, field, None))
            new_value = cls._normalize_value(data.get(field))
            if old_value != new_value:
                fields.append(field)
        return fields

    @staticmethod
    def _read_version(extra_config: Optional[Dict[str, Any]]) -> str:
        if not isinstance(extra_config, dict):
            return DEFAULT_AGENT_VERSION
        raw = extra_config.get("version")
        return UserAgentBaseService._normalize_version(raw if isinstance(raw, str) else "")

    @staticmethod
    def _read_change_history(extra_config: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
        if not isinstance(extra_config, dict):
            return []
        raw_history = extra_config.get("change_history")
        if not isinstance(raw_history, list):
            return []

        history: List[Dict[str, str]] = []
        for item in raw_history:
            if not isinstance(item, dict):
                continue
            timestamp = item.get("timestamp")
            content = item.get("content")
            version = item.get("version")
            operator_name = item.get("operator_name")
            details = item.get("details")
            if not isinstance(timestamp, str) or not isinstance(content, str):
                continue
            history.append(
                {
                    "timestamp": timestamp,
                    "content": content,
                    "version": UserAgentBaseService._normalize_version(
                        version if isinstance(version, str) else ""
                    ),
                    "operator_name": (
                        operator_name
                        if isinstance(operator_name, str) and operator_name.strip()
                        else "未知用户"
                    ),
                    "details": UserAgentBaseService._normalize_change_details(details),
                }
            )
        return history

    @staticmethod
    def _increment_version(version: str) -> str:
        normalized = UserAgentBaseService._normalize_version(version)
        match = re.match(r"^[Vv](\d+)\.(\d+)$", normalized)
        if not match:
            return "V1.1"
        major, minor = (int(part) for part in match.groups())
        return f"V{major}.{minor + 1}"

    @staticmethod
    def _build_change_summary(changed_fields: List[str], data: Dict[str, Any]) -> str:
        if changed_fields == ["is_enabled"]:
            return "启用了子智能体" if bool(data.get("is_enabled")) else "停用了子智能体"

        changed_labels = [VERSIONED_FIELDS[field] for field in changed_fields]
        if not changed_labels:
            return "更新了智能体配置"
        if len(changed_labels) <= 3:
            return f"修改了{'、'.join(changed_labels)}"
        preview = "、".join(changed_labels[:3])
        return f"修改了{preview}等{len(changed_labels)}项"

    @classmethod
    def _build_change_details(
        cls, agent: UserAgent, changed_fields: List[str], data: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        details: List[Dict[str, str]] = []
        for field in changed_fields:
            old_value = cls._stringify_detail_value(field, getattr(agent, field, None))
            new_value = cls._stringify_detail_value(field, data.get(field))
            details.append(
                {
                    "field": VERSIONED_FIELDS[field],
                    "before": old_value,
                    "after": new_value,
                }
            )
        return details

    @staticmethod
    def _stringify_detail_value(field: str, value: Any) -> str:
        if field == "is_enabled":
            return "启用" if bool(value) else "关闭"
        if value is None:
            return "未填写"
        if isinstance(value, list):
            return "、".join(str(item) for item in value) if value else "未填写"
        if isinstance(value, bool):
            return "是" if value else "否"
        text = str(value).strip()
        return text if text else "未填写"

    @staticmethod
    def _normalize_change_details(details: Any) -> List[Dict[str, str]]:
        if not isinstance(details, list):
            return []
        normalized: List[Dict[str, str]] = []
        for item in details:
            if not isinstance(item, dict):
                continue
            field = item.get("field")
            before = item.get("before")
            after = item.get("after")
            if not isinstance(field, str):
                continue
            normalized.append(
                {
                    "field": field,
                    "before": before if isinstance(before, str) else "未填写",
                    "after": after if isinstance(after, str) else "未填写",
                }
            )
        return normalized

    @staticmethod
    def _normalize_version(version: str) -> str:
        if not isinstance(version, str) or not version.strip():
            return DEFAULT_AGENT_VERSION
        raw = version.strip()
        if re.match(r"^[Vv]\d+\.\d+$", raw):
            return f"V{raw[1:]}"
        legacy_patch = re.match(r"^(\d+)\.(\d+)\.(\d+)$", raw)
        if legacy_patch:
            major, minor, patch = (int(part) for part in legacy_patch.groups())
            return f"V{major}.{minor + patch}"
        legacy_minor = re.match(r"^(\d+)\.(\d+)$", raw)
        if legacy_minor:
            major, minor = (int(part) for part in legacy_minor.groups())
            return f"V{major}.{minor}"
        return DEFAULT_AGENT_VERSION

    @staticmethod
    def _merge_extra_config(
        current_extra: Optional[Dict[str, Any]],
        incoming_extra: Optional[Dict[str, Any]],
        *,
        version: str,
        change_history: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        merged = dict(current_extra or {})
        if isinstance(incoming_extra, dict):
            merged.update(incoming_extra)
        merged["version"] = version
        merged["change_history"] = change_history
        return merged
