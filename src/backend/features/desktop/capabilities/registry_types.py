"""Typed installation projections of device capability records."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
from features.desktop.models import DeviceCapabilityInstallation
from core.capabilities.reference import ResourceRef

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
    created_at: Optional[datetime]

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
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def to_installation(row: DeviceCapabilityInstallation) -> Installation:
    from features.desktop.capabilities.session_availability import permits

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
        enabled=bool(row.enabled) and permits(row.profile_id, row.install_id, row.resolved_revision),
        source=row.source,
        source_plugin=row.source_plugin,
        generation=int(row.generation or 0),
        last_error=row.last_error,
        payload=dict(row.payload or {}),
        created_at=row.created_at,
    )
