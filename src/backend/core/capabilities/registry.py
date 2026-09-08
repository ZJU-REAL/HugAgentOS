"""Device installation index: intent + state, kept in the device's business database."""

from __future__ import annotations

import uuid
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

from core.db.engine import SessionLocal
from core.db.models import (
    DeviceCapabilityComponent,
    DeviceCapabilityInstallation,
    DeviceCapabilityNamePreference,
    DeviceCapabilityTransaction,
)
from sqlalchemy.orm import Session

from .ref import ResourceRef


def install_id(kind: str, profile: str, key: str) -> str:
    return f"{kind}:{profile}:{key}"


def preference_id(kind: str, runtime_name: str, user_id: Optional[str] = None) -> str:
    if user_id is None:
        return f"{kind}:{runtime_name}"
    # Bounded key without changing the existing device database schema.
    key = hashlib.sha256((str(user_id) + "\0" + runtime_name).encode()).hexdigest()
    return f"{kind}:user:{key}"


@dataclass
class Installation:
    install_id: str
    profile_id: str
    kind: str
    key: str
    ref: ResourceRef
    display_name: str
    description: str
    version: str
    content_hash: Optional[str]
    resolved_revision: Optional[str]
    state: str
    enabled: bool
    source: str
    source_plugin: Optional[str]
    generation: int
    last_error: Optional[str]
    payload: Dict[str, Any]

    @property
    def ready(self) -> bool:
        return self.state == "ready" and bool(self.resolved_revision)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "install_id": self.install_id,
            "profile_id": self.profile_id,
            "kind": self.kind,
            "key": self.key,
            "resource_ref": self.ref.to_dict(),
            "display_name": self.display_name,
            "description": self.description,
            "version": self.version,
            "content_hash": self.content_hash,
            "resolved_revision": self.resolved_revision,
            "state": self.state,
            "enabled": self.enabled,
            "source": self.source,
            "source_plugin": self.source_plugin,
            "generation": self.generation,
            "last_error": self.last_error,
            "payload": dict(self.payload or {}),
        }


def _to_installation(row: DeviceCapabilityInstallation) -> Installation:
    return Installation(
        install_id=row.install_id,
        profile_id=row.profile_id,
        kind=row.kind,
        key=row.key,
        ref=ResourceRef(row.ref_issuer, row.ref_namespace, row.kind, row.ref_id),
        display_name=row.display_name or "",
        description=row.description or "",
        version=row.version or "",
        content_hash=row.content_hash,
        resolved_revision=row.resolved_revision,
        state=row.state,
        enabled=bool(row.enabled),
        source=row.source,
        source_plugin=row.source_plugin,
        generation=int(row.generation or 0),
        last_error=row.last_error,
        payload=dict(row.payload or {}),
    )


@contextmanager
def _session(db: Optional[Session] = None) -> Iterator[Session]:
    if db is not None:
        yield db
        return
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def upsert(
    *,
    profile_id: str,
    ref: ResourceRef,
    display_name: str = "",
    description: str = "",
    version: str = "",
    content_hash: Optional[str] = None,
    source: str = "cloud",
    source_plugin: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    enabled: Optional[bool] = None,
    db: Optional[Session] = None,
) -> Installation:
    """Create or refresh the intent row. State transitions are done by the setters below."""
    iid = install_id(ref.kind, profile_id, ref.key)
    with _session(db) as s:
        row = s.get(DeviceCapabilityInstallation, iid)
        if row is None:
            row = DeviceCapabilityInstallation(
                install_id=iid,
                profile_id=profile_id,
                kind=ref.kind,
                key=ref.key,
                ref_issuer=ref.issuer,
                ref_namespace=ref.namespace,
                ref_id=ref.id,
                state="pending",
                enabled=True if enabled is None else enabled,
                source=source,
                generation=0,
            )
            s.add(row)
        row.display_name = display_name or row.display_name or ref.id
        row.description = description if description else (row.description or "")
        row.version = version or row.version or ""
        if content_hash is not None and content_hash != row.content_hash:
            previous_hash = row.content_hash
            row.content_hash = content_hash
            if row.state == "ready" and row.resolved_revision:
                # New published content: keep the old revision usable until prepared.
                row.payload = {
                    **dict(row.payload or {}),
                    "resolved_content_hash": (row.payload or {}).get("resolved_content_hash")
                    or previous_hash,
                    "update_available": True,
                }
        row.source = source
        row.source_plugin = source_plugin
        if payload:
            row.payload = {**dict(row.payload or {}), **payload}
        if enabled is not None:
            row.payload = {**dict(row.payload or {}), "source_enabled": bool(enabled)}
            row.enabled = bool(enabled and (row.payload or {}).get("device_enabled_override", True))
        if row.state == "removed":
            row.state = "pending"
        s.flush()
        return _to_installation(row)


def get(install_id_: str, db: Optional[Session] = None) -> Optional[Installation]:
    with _session(db) as s:
        row = s.get(DeviceCapabilityInstallation, install_id_)
        return _to_installation(row) if row else None


def list_installations(
    *,
    kind: Optional[str] = None,
    profile_id: Optional[str] = None,
    profiles: Optional[List[str]] = None,
    include_removed: bool = False,
    db: Optional[Session] = None,
) -> List[Installation]:
    with _session(db) as s:
        q = s.query(DeviceCapabilityInstallation)
        if kind:
            q = q.filter(DeviceCapabilityInstallation.kind == kind)
        if profile_id:
            q = q.filter(DeviceCapabilityInstallation.profile_id == profile_id)
        if profiles is not None:
            q = q.filter(DeviceCapabilityInstallation.profile_id.in_(list(profiles)))
        if not include_removed:
            q = q.filter(DeviceCapabilityInstallation.state != "removed")
        rows = q.order_by(DeviceCapabilityInstallation.kind, DeviceCapabilityInstallation.key).all()
        return [_to_installation(r) for r in rows]


def set_state(
    install_id_: str,
    state: str,
    *,
    resolved_revision: Optional[str] = ...,  # type: ignore[assignment]
    last_error: Optional[str] = None,
    payload_update: Optional[Dict[str, Any]] = None,
    db: Optional[Session] = None,
) -> Optional[Installation]:
    with _session(db) as s:
        row = s.get(DeviceCapabilityInstallation, install_id_)
        if row is None:
            return None
        row.state = state
        if resolved_revision is not ...:
            row.resolved_revision = resolved_revision
        row.last_error = last_error
        if state == "ready":
            row.generation = int(row.generation or 0) + 1
            payload = dict(row.payload or {})
            payload.pop("update_available", None)
            from .paths import revision_for_hash

            if row.content_hash and row.resolved_revision == revision_for_hash(row.content_hash):
                payload["resolved_content_hash"] = row.content_hash
            row.payload = payload
        if payload_update:
            row.payload = {**dict(row.payload or {}), **payload_update}
        s.flush()
        return _to_installation(row)


def set_enabled(
    install_id_: str, enabled: bool, db: Optional[Session] = None
) -> Optional[Installation]:
    with _session(db) as s:
        row = s.get(DeviceCapabilityInstallation, install_id_)
        if row is None:
            return None
        row.enabled = enabled
        s.flush()
        return _to_installation(row)


def mark_removed(install_id_: str, db: Optional[Session] = None) -> bool:
    with _session(db) as s:
        row = s.get(DeviceCapabilityInstallation, install_id_)
        if row is None:
            return False
        row.state = "removed"
        row.resolved_revision = None
        s.flush()
        return True


def delete(install_id_: str, db: Optional[Session] = None) -> bool:
    with _session(db) as s:
        row = s.get(DeviceCapabilityInstallation, install_id_)
        if row is None:
            return False
        s.delete(row)
        s.query(DeviceCapabilityComponent).filter(
            (DeviceCapabilityComponent.owner_install_id == install_id_)
            | (DeviceCapabilityComponent.component_install_id == install_id_)
        ).delete(synchronize_session=False)
        s.flush()
        return True


# ── name preferences ───────────────────────────────────────────────────


def preferences(
    kind: str, db: Optional[Session] = None, *, user_id: Optional[str] = None
) -> Dict[str, str]:
    """Choices for one local user; an absent user means device defaults only.

    Filtering by chosen_by also recognizes legacy rows whose primary key did
    not contain the user, without exposing that choice to another identity.
    """
    with _session(db) as s:
        rows = s.query(DeviceCapabilityNamePreference).filter(
            DeviceCapabilityNamePreference.kind == kind,
            DeviceCapabilityNamePreference.chosen_by == user_id,
        )
        ordered = sorted(
            rows,
            key=lambda row: row.preference_id == preference_id(kind, row.runtime_name, user_id),
        )
        return {r.runtime_name: r.chosen_install_id for r in ordered}


def set_preference(
    kind: str,
    runtime_name: str,
    chosen_install_id: str,
    *,
    chosen_by: Optional[str] = None,
    db: Optional[Session] = None,
) -> None:
    pid = preference_id(kind, runtime_name, chosen_by)
    with _session(db) as s:
        row = s.get(DeviceCapabilityNamePreference, pid)
        if row is None:
            row = DeviceCapabilityNamePreference(
                preference_id=pid, kind=kind, runtime_name=runtime_name
            )
            s.add(row)
        row.chosen_install_id = chosen_install_id
        row.chosen_by = chosen_by
        s.flush()


def clear_preference(
    kind: str, runtime_name: str, db: Optional[Session] = None, *, user_id: Optional[str] = None
) -> bool:
    with _session(db) as s:
        rows = (
            s.query(DeviceCapabilityNamePreference)
            .filter(
                DeviceCapabilityNamePreference.kind == kind,
                DeviceCapabilityNamePreference.runtime_name == runtime_name,
                DeviceCapabilityNamePreference.chosen_by == user_id,
            )
            .all()
        )
        for row in rows:
            s.delete(row)
        s.flush()
        return bool(rows)


# ── components (plugin ownership) ──────────────────────────────────────


def set_components(
    owner_install_id: str, components: Dict[str, bool], db: Optional[Session] = None
) -> None:
    """Replace the ownership edges of a plugin: {component_install_id: required}."""
    with _session(db) as s:
        s.query(DeviceCapabilityComponent).filter(
            DeviceCapabilityComponent.owner_install_id == owner_install_id
        ).delete(synchronize_session=False)
        for cid, required in components.items():
            s.add(
                DeviceCapabilityComponent(
                    edge_id=f"{owner_install_id}->{cid}",
                    owner_install_id=owner_install_id,
                    component_install_id=cid,
                    required=bool(required),
                )
            )
        s.flush()


def components_of(owner_install_id: str, db: Optional[Session] = None) -> Dict[str, bool]:
    with _session(db) as s:
        rows = s.query(DeviceCapabilityComponent).filter(
            DeviceCapabilityComponent.owner_install_id == owner_install_id
        )
        return {r.component_install_id: bool(r.required) for r in rows}


def owners_of(component_install_id: str, db: Optional[Session] = None) -> List[str]:
    with _session(db) as s:
        rows = s.query(DeviceCapabilityComponent.owner_install_id).filter(
            DeviceCapabilityComponent.component_install_id == component_install_id
        )
        return [r[0] for r in rows]


# ── transactions ──────────────────────────────────────────────────────


def begin_transaction(
    install_id_: str, target_generation: int, db: Optional[Session] = None
) -> str:
    tx_id = uuid.uuid4().hex
    with _session(db) as s:
        s.add(
            DeviceCapabilityTransaction(
                tx_id=tx_id,
                install_id=install_id_,
                phase="staged",
                target_generation=target_generation,
                file_inventory=[],
            )
        )
        s.flush()
    return tx_id


def advance_transaction(
    tx_id: str,
    phase: str,
    *,
    inventory: Optional[List[str]] = None,
    error: Optional[str] = None,
    db: Optional[Session] = None,
) -> None:
    with _session(db) as s:
        row = s.get(DeviceCapabilityTransaction, tx_id)
        if row is None:
            return
        row.phase = phase
        if inventory is not None:
            row.file_inventory = list(inventory)
        row.error = error
        s.flush()


def open_transactions(db: Optional[Session] = None) -> List[Dict[str, Any]]:
    with _session(db) as s:
        rows = s.query(DeviceCapabilityTransaction).filter(
            DeviceCapabilityTransaction.phase.in_(("staged", "published"))
        )
        return [
            {
                "tx_id": r.tx_id,
                "install_id": r.install_id,
                "phase": r.phase,
                "target_generation": r.target_generation,
                "file_inventory": list(r.file_inventory or []),
            }
            for r in rows
        ]
