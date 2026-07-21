"""Repository for versioned ontology assets and runtime audit evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.db.models import (
    OntologyDraft,
    OntologyEnforcementEvent,
    OntologyPack,
    OntologyPackVersion,
    OntologyReviewRun,
)
from sqlalchemy import desc
from sqlalchemy.orm import Session


class OntologyRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_packs(self, *, enabled_only: bool = False) -> list[OntologyPack]:
        query = self.db.query(OntologyPack).filter(OntologyPack.deleted_at.is_(None))
        if enabled_only:
            query = query.filter(OntologyPack.is_enabled.is_(True))
        return query.order_by(desc(OntologyPack.is_default), OntologyPack.name).all()

    def get_pack(self, pack_id: str) -> OntologyPack | None:
        return (
            self.db.query(OntologyPack)
            .filter(OntologyPack.pack_id == pack_id, OntologyPack.deleted_at.is_(None))
            .first()
        )

    def create_pack(self, data: dict[str, Any]) -> OntologyPack:
        row = OntologyPack(**data)
        self.db.add(row)
        self.db.flush()
        return row

    def update_pack(self, row: OntologyPack, data: dict[str, Any]) -> OntologyPack:
        for key, value in data.items():
            setattr(row, key, value)
        row.updated_at = datetime.utcnow()
        self.db.flush()
        return row

    def list_versions(self, pack_id: str) -> list[OntologyPackVersion]:
        return (
            self.db.query(OntologyPackVersion)
            .filter(OntologyPackVersion.pack_id == pack_id)
            .order_by(desc(OntologyPackVersion.created_at))
            .all()
        )

    def get_version(self, version_id: str) -> OntologyPackVersion | None:
        return (
            self.db.query(OntologyPackVersion)
            .filter(OntologyPackVersion.version_id == version_id)
            .first()
        )

    def get_pack_version(self, pack_id: str, version: str) -> OntologyPackVersion | None:
        return (
            self.db.query(OntologyPackVersion)
            .filter(
                OntologyPackVersion.pack_id == pack_id,
                OntologyPackVersion.version == version,
            )
            .first()
        )

    def get_working_draft(self, pack_id: str) -> OntologyPackVersion | None:
        return (
            self.db.query(OntologyPackVersion)
            .filter(
                OntologyPackVersion.pack_id == pack_id,
                OntologyPackVersion.status == "draft",
            )
            .order_by(
                desc(OntologyPackVersion.updated_at),
                desc(OntologyPackVersion.created_at),
            )
            .first()
        )

    def get_active_versions(
        self,
        pack_ids: list[str] | None = None,
    ) -> list[OntologyPackVersion]:
        query = (
            self.db.query(OntologyPackVersion)
            .join(OntologyPack, OntologyPack.active_version_id == OntologyPackVersion.version_id)
            .filter(
                OntologyPack.deleted_at.is_(None),
                OntologyPack.is_enabled.is_(True),
                OntologyPackVersion.status == "active",
            )
        )
        if pack_ids:
            query = query.filter(OntologyPack.pack_id.in_(pack_ids))
        else:
            query = query.filter(OntologyPack.is_default.is_(True))
        return query.order_by(OntologyPack.name).all()

    def create_version(self, data: dict[str, Any]) -> OntologyPackVersion:
        row = OntologyPackVersion(**data)
        self.db.add(row)
        self.db.flush()
        return row

    def update_working_draft(
        self,
        row: OntologyPackVersion,
        data: dict[str, Any],
    ) -> OntologyPackVersion:
        for key, value in data.items():
            setattr(row, key, value)
        row.updated_at = datetime.utcnow()
        self.db.flush()
        return row

    def delete_working_draft(self, row: OntologyPackVersion) -> None:
        self.db.delete(row)
        self.db.flush()

    def activate(self, pack: OntologyPack, version: OntologyPackVersion) -> None:
        self.db.query(OntologyPackVersion).filter(
            OntologyPackVersion.pack_id == pack.pack_id,
            OntologyPackVersion.status == "active",
        ).update({OntologyPackVersion.status: "retired"}, synchronize_session=False)
        version.status = "active"
        version.activated_at = datetime.utcnow()
        version.updated_at = datetime.utcnow()
        pack.active_version_id = version.version_id
        pack.is_enabled = True
        pack.updated_at = datetime.utcnow()
        self.db.flush()

    def create_event(self, data: dict[str, Any]) -> OntologyEnforcementEvent:
        row = OntologyEnforcementEvent(**data)
        self.db.add(row)
        self.db.commit()
        return row

    def list_events(
        self,
        *,
        limit: int = 100,
        chat_id: str | None = None,
        decision: str | None = None,
    ) -> list[OntologyEnforcementEvent]:
        query = self.db.query(OntologyEnforcementEvent)
        if chat_id:
            query = query.filter(OntologyEnforcementEvent.chat_id == chat_id)
        if decision:
            query = query.filter(OntologyEnforcementEvent.decision == decision)
        return query.order_by(desc(OntologyEnforcementEvent.created_at)).limit(limit).all()

    def create_review(self, data: dict[str, Any]) -> OntologyReviewRun:
        row = OntologyReviewRun(**data)
        self.db.add(row)
        self.db.commit()
        return row

    def list_reviews(self, *, limit: int = 100) -> list[OntologyReviewRun]:
        return (
            self.db.query(OntologyReviewRun)
            .order_by(desc(OntologyReviewRun.created_at))
            .limit(limit)
            .all()
        )

    def list_drafts(self, *, status: str | None = None, limit: int = 100) -> list[OntologyDraft]:
        query = self.db.query(OntologyDraft)
        if status:
            query = query.filter(OntologyDraft.review_status == status)
        return (
            query.order_by(desc(OntologyDraft.value_score), desc(OntologyDraft.created_at))
            .limit(limit)
            .all()
        )

    def get_draft(self, draft_id: str) -> OntologyDraft | None:
        return self.db.query(OntologyDraft).filter(OntologyDraft.draft_id == draft_id).first()

    def create_draft(self, data: dict[str, Any]) -> OntologyDraft:
        row = OntologyDraft(**data)
        self.db.add(row)
        self.db.commit()
        return row
