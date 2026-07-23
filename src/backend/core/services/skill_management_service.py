"""Shared skill mutation helpers used by admin and self-service routes."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.agent_skills.registry import _split_frontmatter
from core.db.models import AdminMcpServer, AdminSkill
from core.infra.exceptions import BadRequestError
from core.ontology.build_validator import ensure_ontology_build_valid
from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)


def refresh_skill_caches() -> None:
    """Refresh skill caches without turning a committed mutation into an HTTP 500."""
    try:
        from core.agent_skills.cache_refresh import refresh_skill_caches as refresh

        refresh()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skill cache refresh failed after a committed mutation: %s", exc)


def _sanitize_frontmatter_value(value: str) -> str:
    return (value or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()


def _parse_frontmatter_list(skill_content: Optional[str], field_name: str) -> List[str]:
    try:
        frontmatter, _ = _split_frontmatter(skill_content or "")
    except Exception:
        return []
    raw = str(frontmatter.get(field_name, "") or "")
    return list(
        dict.fromkeys(item.strip() for item in raw.replace(",", " ").split() if item.strip())
    )


def extract_mcp_server_ids(skill_content: Optional[str]) -> List[str]:
    """Return MCP bindings persisted in generated ``SKILL.md`` content."""
    return _parse_frontmatter_list(skill_content, "mcp_servers")


def resolve_mcp_bindings(
    db: Session,
    mcp_server_ids: List[str],
    *,
    owner_user_id: Optional[str] = None,
    strict: bool = True,
) -> tuple[List[str], List[str]]:
    """Resolve visible MCP IDs and their discovered tool names."""
    normalized_ids = list(
        dict.fromkeys(str(item).strip() for item in (mcp_server_ids or []) if str(item).strip())
    )
    if not normalized_ids:
        return [], []

    rows = db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(normalized_ids)).all()
    visible_rows: Dict[str, AdminMcpServer] = {}
    for row in rows:
        if row.source_plugin is not None:
            continue
        if row.owner_user_id is None:
            if not row.is_enabled:
                continue
        elif owner_user_id is None or row.owner_user_id != owner_user_id:
            continue
        visible_rows[row.server_id] = row

    unavailable = [server_id for server_id in normalized_ids if server_id not in visible_rows]
    if unavailable and strict:
        raise BadRequestError(
            message="选择的 MCP 不存在、已被管理员停用或无权绑定：" + "、".join(unavailable)
        )

    resolved_ids = [server_id for server_id in normalized_ids if server_id in visible_rows]
    tool_names: List[str] = []
    for server_id in resolved_ids:
        for item in visible_rows[server_id].tools_json or []:
            if not isinstance(item, dict):
                continue
            tool_name = str(item.get("name") or "").strip()
            if tool_name and tool_name not in tool_names:
                tool_names.append(tool_name)
    return resolved_ids, tool_names


def resolve_ontology_workflows(db: Session, tags: List[str]) -> List[str]:
    """Snapshot workflow references activated by controlled skill tags."""
    selected = {tag for tag in tags if isinstance(tag, str) and tag.startswith("ontology:")}
    if not selected:
        return []
    from core.services.ontology_service import OntologyService

    workflow_refs: List[str] = []
    for option in OntologyService(db).list_asset_tag_options("skill"):
        if option.get("value") not in selected:
            continue
        for workflow in option.get("workflows") or []:
            workflow_ref = str(workflow.get("workflow_ref") or "").strip()
            if workflow_ref and workflow_ref not in workflow_refs:
                workflow_refs.append(workflow_ref)
    return workflow_refs


def build_skill_content(
    skill_id: str,
    display_name: str,
    description: str,
    version: str,
    tags: List[str],
    allowed_tools: List[str],
    instructions: str,
    mcp_server_ids: Optional[List[str]] = None,
    ontology_workflows: Optional[List[str]] = None,
) -> str:
    """Build a complete ``SKILL.md`` string from editable fields."""
    frontmatter = [
        "---",
        f"name: {skill_id}",
        f"display_name: {_sanitize_frontmatter_value(display_name)}",
        f"description: {_sanitize_frontmatter_value(description)}",
        f"version: {_sanitize_frontmatter_value(version)}",
    ]
    if tags:
        frontmatter.append(f"tags: {', '.join(tags)}")
    ontology_tags = [tag for tag in tags if tag.startswith("ontology:")]
    if ontology_tags:
        frontmatter.append(f"ontology_tags: {', '.join(ontology_tags)}")
    if ontology_workflows:
        frontmatter.append(f"ontology_workflows: {', '.join(ontology_workflows)}")
    if mcp_server_ids:
        frontmatter.append(f"mcp_servers: {' '.join(mcp_server_ids)}")
    if allowed_tools:
        frontmatter.append(f"allowed_tools: {' '.join(allowed_tools)}")
    return "\n".join([*frontmatter, "---", "", instructions, ""])


def validate_skill_file_path(filename: str) -> str:
    """Validate and normalize a relative skill-file path."""
    name = (filename or "").strip().strip("/")
    if not name:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    if "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail=f"非法文件名: {filename}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise HTTPException(status_code=400, detail=f"非法文件路径: {filename}")
    return name


def extract_instructions(skill_content: Optional[str]) -> str:
    """Return the Markdown body after the ``SKILL.md`` frontmatter."""
    try:
        _, body = _split_frontmatter(skill_content or "")
    except Exception:
        return ""
    return (body or "").strip()


def parse_and_upsert_skill_zip(
    db: Session,
    data: bytes,
    *,
    owner_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse a skill zip and upsert a global or owner-isolated DB skill."""
    from core.services.marketplace_service import parse_skill_zip

    parsed = parse_skill_zip(data)
    skill_id = parsed["skill_id"]
    raw = parsed["skill_content"]
    meta = parsed["meta"]
    extra_files = parsed["extra_files"]
    dependencies = parsed["dependencies"]
    skipped = parsed["skipped"]

    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=meta.name or skill_id,
        description=meta.description or "",
        instructions=extract_instructions(raw),
        tool_names=list(meta.allowed_tools or []),
        ontology_tags=list(meta.tags or []),
    )

    now = datetime.utcnow()
    existing = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
    if existing is not None:
        if owner_user_id is not None and existing.owner_user_id != owner_user_id:
            if existing.owner_user_id is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"技能 id 「{skill_id}」与公共技能冲突，请改名",
                )
            raise HTTPException(status_code=409, detail=f"技能 id 「{skill_id}」已被占用")
        existing.skill_content = raw
        existing.display_name = meta.name
        existing.description = meta.description
        existing.version = meta.version
        existing.tags = meta.tags
        existing.allowed_tools = meta.allowed_tools
        existing.extra_files = extra_files
        existing.dependencies = dependencies
        existing.is_enabled = True
        existing.updated_at = now
        flag_modified(existing, "extra_files")
        flag_modified(existing, "dependencies")
    else:
        db.add(
            AdminSkill(
                skill_id=skill_id,
                skill_content=raw,
                display_name=meta.name,
                description=meta.description,
                version=meta.version,
                tags=meta.tags,
                allowed_tools=meta.allowed_tools,
                extra_files=extra_files,
                dependencies=dependencies,
                is_enabled=True,
                owner_user_id=owner_user_id,
                created_at=now,
                updated_at=now,
            )
        )
    db.commit()

    refresh_skill_caches()
    logger.info(
        "skill_uploaded: %s (owner=%s, %d files stored, %d skipped; deps: %d pip / %d npm / %d apt)",
        skill_id,
        owner_user_id or "global",
        len(extra_files),
        len(skipped),
        len(dependencies.get("pip", [])),
        len(dependencies.get("npm", [])),
        len(dependencies.get("apt", [])),
    )
    return {
        "id": skill_id,
        "dependencies": dependencies,
        "stored_files": len(extra_files),
        "skipped": skipped,
        "message": "Skill uploaded",
    }
