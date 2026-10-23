"""Manage ontology packs, drafts and versions, and resolve asset tags."""
from __future__ import annotations
from core.infra.time import utc_now
import hashlib
import json
import uuid
from typing import Any
from core.config.display_names import TOOL_DISPLAY_NAMES
from core.db.models import AdminMcpServer, AdminSkill, OntologyDraft, OntologyPack, OntologyPackVersion
from core.db.repository import OntologyRepository
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.ontology.schemas import OntologyPackDocument
from core.ontology.validator import DomainPackValidator
from core.ontology.runtime_pack import build_runtime_payload
from sqlalchemy import or_
from sqlalchemy.orm import Session


def normalize_ontology_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        return []
    return sorted({str(item).strip() for item in items if str(item).strip()})


class OntologyService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = OntologyRepository(db)
        self.validator = DomainPackValidator()

    def validate_document(
        self,
        payload: dict[str, Any],
    ) -> tuple[OntologyPackDocument | None, dict[str, Any]]:
        schemas, known = self._known_tool_schemas()
        document, report = self.validator.validate(
            payload,
            tool_schemas=schemas,
            known_tools=known,
        )
        return document, report.as_dict()

    def create_version(
        self,
        payload: dict[str, Any],
        *,
        actor_id: str | None = None,
        activate: bool = False,
    ):
        document, report = self.validate_document(payload)
        if document is None or not report["valid"]:
            raise BadRequestError("Domain Pack 校验失败", data=report)
        existing = self.repo.get_pack_version(document.pack_id, document.version)
        if existing:
            raise BadRequestError(
                f"Domain Pack {document.pack_id} 的版本 {document.version} 已存在"
            )
        pack = self.repo.get_pack(document.pack_id)
        if pack is not None and self.repo.get_working_draft(document.pack_id) is not None:
            raise BadRequestError("该领域包已有工作草稿，请先发布或放弃草稿后再导入新版本")
        if pack is None:
            pack = self.repo.create_pack(
                {
                    "pack_id": document.pack_id,
                    "name": document.name,
                    "domain": document.domain,
                    "description": document.description,
                    "is_default": False,
                    "created_by": actor_id,
                }
            )
        canonical = document.model_dump(mode="json", by_alias=True)
        checksum = self._checksum(canonical)
        version = self.repo.create_version(
            {
                "version_id": f"ontov_{uuid.uuid4().hex[:16]}",
                "pack_id": document.pack_id,
                "version": document.version,
                "content": canonical,
                "checksum": checksum,
                "status": "draft",
                "validation_report": report,
                "created_by": actor_id,
            }
        )
        if activate:
            self._sync_pack_metadata(pack, document)
            self.repo.activate(pack, version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def save_working_draft(
        self,
        pack_id: str,
        payload: dict[str, Any],
        *,
        draft_version_id: str | None = None,
        expected_checksum: str | None = None,
        actor_id: str | None = None,
    ) -> tuple[OntologyPackVersion, bool]:
        """Create the only working draft for a pack, or update that draft in place."""

        pack = self.repo.get_pack(pack_id)
        if pack is None:
            raise ResourceNotFoundError("ontology_pack", pack_id)
        document, report = self.validate_document(payload)
        if document is None or not report["valid"]:
            raise BadRequestError("Domain Pack 校验失败", data=report)
        if document.pack_id != pack_id:
            raise BadRequestError("请求路径与 Domain Pack 文档中的 pack_id 不一致")

        canonical = document.model_dump(mode="json", by_alias=True)
        checksum = self._checksum(canonical)
        working_draft = self.repo.get_working_draft(pack_id)
        created = working_draft is None

        if working_draft is None:
            if draft_version_id is not None:
                raise BadRequestError("工作草稿已不存在，请刷新后重试")
            existing = self.repo.get_pack_version(pack_id, document.version)
            if existing is not None:
                raise BadRequestError(f"Domain Pack {pack_id} 的版本 {document.version} 已存在")
            working_draft = self.repo.create_version(
                {
                    "version_id": f"ontov_{uuid.uuid4().hex[:16]}",
                    "pack_id": pack_id,
                    "version": document.version,
                    "content": canonical,
                    "checksum": checksum,
                    "status": "draft",
                    "validation_report": report,
                    "created_by": actor_id,
                }
            )
        else:
            if draft_version_id != working_draft.version_id:
                raise BadRequestError("该领域包已有工作草稿，请刷新后在现有草稿上继续编辑")
            if not expected_checksum:
                raise BadRequestError("更新工作草稿时必须提供最新校验和")
            if expected_checksum != working_draft.checksum:
                raise BadRequestError("工作草稿已被其他管理员更新，请刷新后重新编辑")
            if document.version != working_draft.version:
                raise BadRequestError("工作草稿创建后不能修改版本号")
            self.repo.update_working_draft(
                working_draft,
                {
                    "content": canonical,
                    "checksum": checksum,
                    "validation_report": report,
                },
            )

        if pack.active_version_id is None:
            self._sync_pack_metadata(pack, document)
        self.db.commit()
        self.db.refresh(working_draft)
        return working_draft, created

    def discard_working_draft(self, pack_id: str, version_id: str) -> None:
        """Discard an unpublished draft and release linked evolution candidates."""

        pack = self.repo.get_pack(pack_id)
        if pack is None:
            raise ResourceNotFoundError("ontology_pack", pack_id)
        working_draft = self.repo.get_working_draft(pack_id)
        if working_draft is None or working_draft.version_id != version_id:
            raise ResourceNotFoundError("ontology_working_draft", version_id)

        now = utc_now()
        linked_candidates = (
            self.db.query(OntologyDraft).filter(OntologyDraft.pack_id == pack_id).all()
        )
        for candidate in linked_candidates:
            proposal = dict(candidate.proposal or {})
            if proposal.get("materialized_version_id") != version_id:
                continue
            proposal.pop("materialized_version_id", None)
            candidate.proposal = proposal
            candidate.updated_at = now
        self.repo.delete_working_draft(working_draft)
        self.db.commit()

    def activate(self, pack_id: str, version_id: str):
        pack = self.repo.get_pack(pack_id)
        if not pack:
            raise ResourceNotFoundError("ontology_pack", pack_id)
        version = self.repo.get_version(version_id)
        if not version or version.pack_id != pack_id:
            raise ResourceNotFoundError("ontology_pack_version", version_id)
        if not (version.validation_report or {}).get("valid", False):
            raise BadRequestError("未通过校验的 Domain Pack 版本不能激活")
        if version.status == "active" and pack.active_version_id == version_id:
            return version
        document = OntologyPackDocument.model_validate(version.content)
        self._sync_pack_metadata(pack, document)
        self.repo.activate(pack, version)
        self.db.commit()
        return version

    @staticmethod
    def _checksum(canonical: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def _sync_pack_metadata(self, pack: OntologyPack, document: OntologyPackDocument) -> None:
        self.repo.update_pack(
            pack,
            {
                "name": document.name,
                "domain": document.domain,
                "description": document.description,
            },
        )

    def set_pack_flags(
        self,
        pack_id: str,
        *,
        is_enabled: bool | None = None,
        is_default: bool | None = None,
    ):
        pack = self.repo.get_pack(pack_id)
        if not pack:
            raise ResourceNotFoundError("ontology_pack", pack_id)
        if is_default:
            self.db.query(type(pack)).filter(type(pack).pack_id != pack_id).update(
                {type(pack).is_default: False}, synchronize_session=False
            )
        data: dict[str, Any] = {}
        if is_enabled is not None:
            data["is_enabled"] = is_enabled
        if is_default is not None:
            data["is_default"] = is_default
        self.repo.update_pack(pack, data)
        self.db.commit()
        self.db.refresh(pack)
        return pack

    def build_runtime(
        self,
        *,
        task: str,
        pack_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        documents: list[OntologyPackDocument] = []
        version_ids: list[str] = []
        version_id_by_pack: dict[str, str] = {}
        for version in self.repo.get_active_versions(pack_ids):
            try:
                document = OntologyPackDocument.model_validate(version.content)
                documents.append(document)
                version_ids.append(version.version_id)
                version_id_by_pack[document.pack_id] = version.version_id
            except Exception:
                continue
        runtime = build_runtime_payload(documents, task)
        runtime["version_ids"] = version_ids
        for pack in runtime.get("packs", []):
            pack["version_id"] = version_id_by_pack.get(pack.get("pack_id"))
        for candidate in runtime.get("activation_candidates", []):
            version_id = version_id_by_pack.get(candidate.get("pack_id"))
            candidate["version_id"] = version_id
            if candidate.get("pack") is not None:
                candidate["pack"]["version_id"] = version_id
        return runtime

    def list_asset_tag_options(self, asset_kind: str) -> list[dict[str, Any]]:
        """List controlled concept tags that actually activate workflows for one asset kind.

        A concept existing in a Domain Pack does not by itself imply runtime activation.  Only
        ``workflow.asset_triggers[].tags_any`` creates that link, so the form selector must be
        built from those declarations instead of exposing every concept indiscriminately.
        """
        if asset_kind not in {"tool", "skill", "subagent"}:
            raise BadRequestError("不支持的本体资产类型")

        entries: dict[str, dict[str, Any]] = {}
        for version in self.repo.get_active_versions():
            try:
                document = OntologyPackDocument.model_validate(version.content)
            except Exception:
                continue
            concepts = {f"ontology:{concept.id}": concept for concept in document.concepts}
            for workflow in document.workflows:
                for trigger in workflow.asset_triggers:
                    if trigger.kind != asset_kind:
                        continue
                    for value in trigger.tags_any:
                        concept = concepts.get(value)
                        if concept is None:
                            continue
                        entry = entries.setdefault(
                            value,
                            {
                                "value": value,
                                "concept_id": concept.id,
                                "concept_name": concept.name,
                                "definition": concept.definition,
                                "risk": concept.risk,
                                "packs": [],
                                "workflows": [],
                            },
                        )
                        pack_info = {
                            "pack_id": document.pack_id,
                            "pack_name": document.name,
                            "domain": document.domain,
                            "version": document.version,
                        }
                        if pack_info not in entry["packs"]:
                            entry["packs"].append(pack_info)
                        workflow_info = {
                            "workflow_ref": f"{document.pack_id}:{workflow.id}",
                            "workflow_name": workflow.name,
                            "review_level": workflow.review_level,
                            "risk": workflow.risk,
                        }
                        if workflow_info not in entry["workflows"]:
                            entry["workflows"].append(workflow_info)

        for entry in entries.values():
            entry["packs"].sort(key=lambda item: (item["pack_name"], item["version"]))
            entry["workflows"].sort(key=lambda item: item["workflow_ref"])
        return sorted(entries.values(), key=lambda item: (item["concept_name"], item["value"]))

    def resolve_asset_tags(
        self,
        *,
        kind: str,
        asset_id: str,
        user_id: str,
    ) -> list[str]:
        """Resolve trusted ontology tags for one asset immediately before use.

        Only metadata columns are selected. Global assets must be enabled;
        private assets are visible only to their owner and may still be used by
        an explicit binding even when their personal catalog switch is off.
        """
        if kind == "skill":
            owner_filter = (
                or_(
                    AdminSkill.owner_user_id.is_(None),
                    AdminSkill.owner_user_id == user_id,
                )
                if user_id
                else AdminSkill.owner_user_id.is_(None)
            )
            enabled_filter = or_(
                AdminSkill.owner_user_id.isnot(None),
                AdminSkill.is_enabled.is_(True),
            )
            row = (
                self.db.query(AdminSkill.tags)
                .filter(
                    AdminSkill.skill_id == asset_id,
                    owner_filter,
                    enabled_filter,
                )
                .one_or_none()
            )
            if row:
                return normalize_ontology_tags(row.tags)
            # A DB row that exists but is outside this user's scope must never
            # fall through to the process-global loader (which indexes private
            # skills for materialization). Only true filesystem/built-in skills
            # may use the loader metadata fallback.
            db_row_exists = (
                self.db.query(AdminSkill.skill_id).filter(AdminSkill.skill_id == asset_id).first()
                is not None
            )
            if db_row_exists:
                return []
            from core.capabilities.skills.loader_factory import get_skill_loader

            metadata = get_skill_loader().load_all_metadata().get(asset_id)
            return normalize_ontology_tags(getattr(metadata, "tags", []))

        if kind == "tool":
            owner_filter = (
                or_(
                    AdminMcpServer.owner_user_id.is_(None),
                    AdminMcpServer.owner_user_id == user_id,
                )
                if user_id
                else AdminMcpServer.owner_user_id.is_(None)
            )
            enabled_filter = or_(
                AdminMcpServer.owner_user_id.isnot(None),
                AdminMcpServer.is_enabled.is_(True),
            )
            rows = (
                self.db.query(AdminMcpServer.extra_config, AdminMcpServer.tools_json)
                .filter(owner_filter, enabled_filter)
                .all()
            )
            tags: set[str] = set()
            for row in rows:
                extra = row.extra_config or {}
                tool_tag_map = (
                    extra.get("tool_tags") if isinstance(extra.get("tool_tags"), dict) else {}
                )
                for tool in row.tools_json or []:
                    if not isinstance(tool, dict) or str(tool.get("name") or "") != asset_id:
                        continue
                    tags.update(normalize_ontology_tags(extra.get("ontology_tags")))
                    tags.update(normalize_ontology_tags(tool.get("tags")))
                    tags.update(normalize_ontology_tags(tool.get("ontology_tags")))
                    tags.update(normalize_ontology_tags(tool_tag_map.get(asset_id)))
            return sorted(tags)

        if kind == "subagent":
            from core.capabilities.agents.edition import UserAgentService

            try:
                agent = UserAgentService(self.db).get_by_id(asset_id, user_id=user_id or None)
            except (LookupError, PermissionError):
                return []
            return normalize_ontology_tags(agent.get("ontology_tags"))

        return []

    def _known_tool_schemas(self) -> tuple[dict[str, dict[str, Any]], set[str]]:
        known = set(TOOL_DISPLAY_NAMES)
        schemas: dict[str, dict[str, Any]] = {}
        rows = self.db.query(AdminMcpServer).all()
        for row in rows:
            for tool in row.tools_json or []:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                name = str(tool["name"])
                known.add(name)
                if tool.get("inputSchema"):
                    schemas[name] = {"inputSchema": tool["inputSchema"]}
        return schemas, known
