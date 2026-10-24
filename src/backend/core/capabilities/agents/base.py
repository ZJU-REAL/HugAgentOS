"""CRUD, versioning and permission checks for user-defined agents."""
from __future__ import annotations
from core.capabilities.agents import available_resources
from core.capabilities.agents import version_history
import uuid
from typing import Any, Dict, List, Optional
from core.db.models import UserAgent
from core.db.repository import AuditLogRepository, UserAgentRepository
from core.harness.registry.build_governance import ensure_ontology_build_valid
from sqlalchemy.orm import Session
import logging
from core.capabilities.agents import base_constants

logger = logging.getLogger(__name__)


def desktop_store() -> bool:
    """Whether this process keeps a desktop capability file store (agents projected there)."""
    from core.harness.registry.run_capabilities import has_run_capabilities

    return has_run_capabilities()


class UserAgentBaseService:
    """Service for user agent CRUD and permission checks."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = UserAgentRepository(db)

    # ── Queries ──────────────────────────────────────────────────────

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        agents = self.repo.list_for_user(user_id)
        rows = [self._serialize(a) for a in agents]
        if desktop_store():
            # Desktop: the bridged account's stored agents join the device's own;
            # a display-name clash is decided by the resolver, never by order.
            from core.harness.registry.local_publication import local_publication

            return local_publication().merge_visible_agents(user_id, rows)
        return rows

    def list_admin(self) -> List[Dict[str, Any]]:
        agents = self.repo.list_admin()
        return [self._serialize(a) for a in agents]

    def get_by_id(self, agent_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        agent = self.repo.get_by_id(agent_id)
        if not agent:
            stored = self._stored_account_agent(agent_id, user_id)
            if stored is not None:
                return stored.to_serialized()
            raise LookupError(f"Agent {agent_id} not found")
        if user_id and not self._is_accessible(agent, user_id):
            raise PermissionError("No access to this agent")
        return self._serialize(agent)

    def get_raw_by_id(self, agent_id: str, user_id: Optional[str] = None) -> UserAgent:
        """Return the ORM object (for direct use by workflow/factory).

        On a desktop the bridged account's stored agents are a second, equally
        authoritative source: an ``AgentDefinition`` with the same attribute
        surface is returned for them.
        """
        agent = self.repo.get_by_id(agent_id)
        if not agent:
            stored = self._stored_account_agent(agent_id, user_id)
            if stored is not None:
                return stored  # type: ignore[return-value]
            raise LookupError(f"Agent {agent_id} not found")
        if user_id and not self._is_accessible(agent, user_id):
            raise PermissionError("No access to this agent")
        return agent

    @staticmethod
    def _stored_account_agent(agent_id: str, user_id: Optional[str] = None):
        if not desktop_store():
            return None
        from core.harness.registry.local_publication import local_publication
        from core.harness.registry.execution_account import account_authorized_for

        if not account_authorized_for(user_id):
            return None
        return local_publication().account_agent_definition(agent_id)

    def _project_to_store(self, agent: UserAgent) -> None:
        if not desktop_store():
            return
        from core.harness.registry.local_publication import local_publication

        local_publication().publish_local_agent(self._serialize(agent))

    @staticmethod
    def _remove_from_store(agent_id: str) -> None:
        if not desktop_store():
            return
        from core.harness.registry.local_publication import local_publication

        local_publication().remove_local_agent(agent_id)

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
                "version": base_constants.DEFAULT_AGENT_VERSION,
                "timestamp": created_at,
                "content": "创建了子智能体",
                "operator_name": operator_name or user_id or "未知用户",
                "details": [],
            }
        ]
        extra_config = self._merge_extra_config(
            current_extra=None,
            incoming_extra=incoming_extra,
            version=base_constants.DEFAULT_AGENT_VERSION,
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
        self._project_to_store(agent)
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
        versioned_fields = [field for field in changed_fields if field not in base_constants.NON_VERSIONED_FIELDS]
        changed_labels = [base_constants.VERSIONED_FIELDS[field] for field in changed_fields]
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
            change_history = change_history[-base_constants.MAX_CHANGE_HISTORY:]

        payload = dict(data)
        payload["extra_config"] = self._merge_extra_config(
            current_extra=current_extra,
            incoming_extra=incoming_extra,
            version=next_version,
            change_history=change_history,
        )

        agent = self.repo.update(agent_id, payload)
        self._project_to_store(agent)
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
        if ok:
            self._remove_from_store(agent_id)
        self._audit(user_id, "agent.delete", agent_id)
        return ok

    def toggle_enabled(self, agent_id: str) -> Dict[str, Any]:
        agent = self.repo.get_by_id(agent_id)
        if not agent:
            raise LookupError(f"Agent {agent_id} not found")
        new_val = not agent.is_enabled
        agent = self.repo.update(agent_id, {"is_enabled": new_val})
        self._project_to_store(agent)
        return self._serialize(agent)

    # ── Available resources ──────────────────────────────────────────

    def list_available_resources(self, owner_user_id: Optional[str] = None) -> Dict[str, Any]:
        return available_resources.list_available_resources(self, owner_user_id)

    def _ontology_tag_options(self) -> List[Dict[str, Any]]:
        """Controlled labels that activate a sub-agent ontology workflow at runtime."""
        from core.harness.registry.build_governance import list_asset_tag_options

        return list_asset_tag_options(self.db, "subagent")

    # ── Helpers ───────────────────────────────────────────────────────

    def _validate_create_scope(
        self, user_id: Optional[str], owner_type: str, scope_id: Optional[str]
    ) -> None:
        if owner_type == "user":
            if not user_id:
                raise ValueError("user_id required for user agents")
            if self.repo.count_user_agents(user_id) >= base_constants.MAX_USER_AGENTS:
                raise ValueError(f"Maximum {base_constants.MAX_USER_AGENTS} agents per user reached")
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
        return version_history.normalize_value(value)

    @staticmethod
    def _now_iso() -> str:
        return version_history.now_iso()

    @classmethod
    def _collect_changed_fields(cls, agent: UserAgent, data: Dict[str, Any]) -> List[str]:
        return version_history.collect_changed_fields(cls, agent, data)

    @staticmethod
    def _read_version(extra_config: Optional[Dict[str, Any]]) -> str:
        return version_history.read_version(UserAgentBaseService, extra_config)

    @staticmethod
    def _read_change_history(extra_config: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
        return version_history.read_change_history(UserAgentBaseService, extra_config)

    @staticmethod
    def _increment_version(version: str) -> str:
        return version_history.increment_version(UserAgentBaseService, version)

    @staticmethod
    def _build_change_summary(changed_fields: List[str], data: Dict[str, Any]) -> str:
        return version_history.build_change_summary(changed_fields, data)

    @classmethod
    def _build_change_details(
        cls, agent: UserAgent, changed_fields: List[str], data: Dict[str, Any]
    ) -> List[Dict[str, str]]:
        return version_history.build_change_details(cls, agent, changed_fields, data)

    @staticmethod
    def _stringify_detail_value(field: str, value: Any) -> str:
        return version_history.stringify_detail_value(field, value)

    @staticmethod
    def _normalize_change_details(details: Any) -> List[Dict[str, str]]:
        return version_history.normalize_change_details(details)

    @staticmethod
    def _normalize_version(version: str) -> str:
        return version_history.normalize_version(version)

    @staticmethod
    def _merge_extra_config(
        current_extra: Optional[Dict[str, Any]],
        incoming_extra: Optional[Dict[str, Any]],
        *,
        version: str,
        change_history: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        return version_history.merge_extra_config(current_extra, incoming_extra, version=version, change_history=change_history)
