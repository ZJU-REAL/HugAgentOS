"""Repository layer for model_providers and model_role_assignments."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from core.db.models import ModelProvider, ModelRoleAssignment


# ── Predefined roles ─────────────────────────────────────────────────────────

ROLE_DEFINITIONS: dict[str, dict] = {
    "main_agent": {"label": "主智能体推理", "type": "chat"},
    "summarizer": {"label": "标题摘要 + 分类", "type": "chat"},
    "followup":   {"label": "追问生成", "type": "chat"},
    "memory":     {"label": "记忆提取 (mem0)", "type": "chat"},
    "embedding":  {"label": "文本向量化", "type": "embedding"},
    "reranker":   {"label": "搜索结果重排序", "type": "reranker"},
    "chart":      {"label": "图表代码生成", "type": "chat"},
    "plan_agent": {"label": "计划模式推理", "type": "chat"},
    "code_exec":  {"label": "代码执行推理", "type": "chat"},
}


# ── Provider CRUD ─────────────────────────────────────────────────────────────

def list_providers(db: Session) -> list[ModelProvider]:
    return db.query(ModelProvider).order_by(ModelProvider.created_at.desc()).all()


def get_provider(db: Session, provider_id: str) -> Optional[ModelProvider]:
    return db.query(ModelProvider).filter(ModelProvider.provider_id == provider_id).first()


def create_provider(db: Session, *, display_name: str, provider_type: str,
                    base_url: str, api_key: str, model_name: str,
                    provider: str = "openai_compatible",
                    gateway_group: str | None = None,
                    weight: int = 1, priority: int = 0,
                    extra_config: dict | None = None, is_active: bool = True) -> ModelProvider:
    provider_row = ModelProvider(
        provider_id=str(uuid.uuid4()),
        display_name=display_name,
        provider_type=provider_type,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        gateway_group=(gateway_group or None),
        weight=weight,
        priority=priority,
        extra_config=extra_config or {},
        is_active=is_active,
    )
    db.add(provider_row)
    db.commit()
    db.refresh(provider_row)
    return provider_row


def update_provider(db: Session, provider_id: str, **fields) -> Optional[ModelProvider]:
    provider = get_provider(db, provider_id)
    if provider is None:
        return None
    for key, val in fields.items():
        if val is not None and hasattr(provider, key):
            setattr(provider, key, val)
    provider.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(provider)
    return provider


def delete_provider(db: Session, provider_id: str) -> bool:
    """Delete provider. Returns False if not found."""
    provider = get_provider(db, provider_id)
    if provider is None:
        return False
    db.delete(provider)
    db.commit()
    return True


def provider_is_referenced(db: Session, provider_id: str) -> list[str]:
    """Return role_keys that reference this provider."""
    rows = (
        db.query(ModelRoleAssignment.role_key)
        .filter(ModelRoleAssignment.provider_id == provider_id)
        .all()
    )
    return [r.role_key for r in rows]


def set_provider_test_result(db: Session, provider_id: str, success: bool) -> None:
    provider = get_provider(db, provider_id)
    if provider is None:
        return
    provider.last_tested_at = datetime.utcnow()
    provider.last_test_status = "success" if success else "failure"
    db.commit()


# ── Role assignment CRUD ──────────────────────────────────────────────────────

def list_role_assignments(db: Session) -> list[dict]:
    """Return all roles (including unassigned) with their provider info."""
    assignments = (
        db.query(ModelRoleAssignment)
        .options(joinedload(ModelRoleAssignment.provider))
        .all()
    )
    assignment_map = {a.role_key: a for a in assignments}

    result = []
    for role_key, role_def in ROLE_DEFINITIONS.items():
        entry: dict = {
            "role_key": role_key,
            "label": role_def["label"],
            "required_type": role_def["type"],
            "provider_id": None,
            "provider_name": None,
            "model_name": None,
            "updated_at": None,
            "updated_by": None,
        }
        a = assignment_map.get(role_key)
        if a and a.provider:
            entry["provider_id"] = a.provider_id
            entry["provider_name"] = a.provider.display_name
            entry["model_name"] = a.provider.model_name
            entry["updated_at"] = a.updated_at.isoformat() if a.updated_at else None
            entry["updated_by"] = a.updated_by
        result.append(entry)
    return result


def assign_role(db: Session, role_key: str, provider_id: str, updated_by: str = "admin") -> bool:
    """Assign a provider to a role. Returns False if role_key invalid or provider not found."""
    if role_key not in ROLE_DEFINITIONS:
        return False
    provider = get_provider(db, provider_id)
    if provider is None:
        return False

    existing = db.query(ModelRoleAssignment).filter(
        ModelRoleAssignment.role_key == role_key
    ).first()
    if existing:
        existing.provider_id = provider_id
        existing.updated_at = datetime.utcnow()
        existing.updated_by = updated_by
    else:
        db.add(ModelRoleAssignment(
            role_key=role_key,
            provider_id=provider_id,
            updated_at=datetime.utcnow(),
            updated_by=updated_by,
        ))
    db.commit()
    return True


def unassign_role(db: Session, role_key: str) -> bool:
    row = db.query(ModelRoleAssignment).filter(ModelRoleAssignment.role_key == role_key).first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


# ── Export / Import ───────────────────────────────────────────────────────────

def export_all(db: Session) -> dict:
    """Export both tables as JSON-serialisable dicts."""
    providers = list_providers(db)
    assignments = db.query(ModelRoleAssignment).all()

    return {
        "providers": [
            {
                "provider_id": p.provider_id,
                "display_name": p.display_name,
                "provider_type": p.provider_type,
                "provider": getattr(p, "provider", "openai_compatible"),
                "base_url": p.base_url,
                "api_key": p.api_key,
                "model_name": p.model_name,
                "gateway_group": getattr(p, "gateway_group", None),
                "weight": getattr(p, "weight", 1),
                "priority": getattr(p, "priority", 0),
                "extra_config": p.extra_config or {},
                "is_active": p.is_active,
            }
            for p in providers
        ],
        "role_assignments": [
            {
                "role_key": a.role_key,
                "provider_id": a.provider_id,
            }
            for a in assignments
        ],
    }


def import_all(db: Session, data: dict, overwrite: bool = True) -> dict:
    """Import providers + role assignments. Returns counts."""
    imported_providers = 0
    imported_roles = 0

    for p in data.get("providers", []):
        existing = get_provider(db, p["provider_id"])
        if existing and not overwrite:
            continue
        if existing:
            for key in ("display_name", "provider_type", "provider", "base_url", "api_key",
                        "model_name", "gateway_group", "weight", "priority", "extra_config", "is_active"):
                if key in p:
                    setattr(existing, key, p[key])
            existing.updated_at = datetime.utcnow()
        else:
            db.add(ModelProvider(
                provider_id=p["provider_id"],
                display_name=p["display_name"],
                provider_type=p["provider_type"],
                provider=p.get("provider", "openai_compatible"),
                base_url=p["base_url"],
                api_key=p["api_key"],
                model_name=p["model_name"],
                gateway_group=p.get("gateway_group") or None,
                weight=p.get("weight", 1),
                priority=p.get("priority", 0),
                extra_config=p.get("extra_config", {}),
                is_active=p.get("is_active", True),
            ))
        imported_providers += 1

    db.flush()

    for a in data.get("role_assignments", []):
        role_key = a["role_key"]
        if role_key not in ROLE_DEFINITIONS:
            continue
        existing = db.query(ModelRoleAssignment).filter(
            ModelRoleAssignment.role_key == role_key
        ).first()
        if existing:
            existing.provider_id = a["provider_id"]
            existing.updated_at = datetime.utcnow()
            existing.updated_by = "import"
        else:
            db.add(ModelRoleAssignment(
                role_key=role_key,
                provider_id=a["provider_id"],
                updated_by="import",
            ))
        imported_roles += 1

    db.commit()
    return {"imported_providers": imported_providers, "imported_roles": imported_roles}
