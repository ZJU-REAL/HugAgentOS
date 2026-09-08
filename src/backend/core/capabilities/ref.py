"""Resource identity. A name, an install id or a folder is never an identity."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

from .paths import KINDS, LOCAL_PROFILE, safe_segment

LOCAL_ISSUER = "local"
LOCAL_NAMESPACE = "device"
BUILTIN_ISSUER = "builtin"
BUILTIN_NAMESPACE = "bundle"


@dataclass(frozen=True)
class ResourceRef:
    issuer: str
    namespace: str
    kind: str
    id: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown capability kind: {self.kind!r}")
        for field in ("issuer", "namespace", "id"):
            value = getattr(self, field)
            if not value or "/" in value or "\\" in value or any(c.isspace() for c in value):
                raise ValueError(f"invalid ResourceRef.{field}: {value!r}")

    @property
    def key(self) -> str:
        """Directory key inside a profile; validated to be a safe path segment."""
        return safe_segment(self.id)

    def to_dict(self) -> Dict[str, str]:
        return {
            "issuer": self.issuer,
            "namespace": self.namespace,
            "kind": self.kind,
            "id": self.id,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ResourceRef":
        return cls(
            issuer=str(raw.get("issuer") or ""),
            namespace=str(raw.get("namespace") or ""),
            kind=str(raw.get("kind") or ""),
            id=str(raw.get("id") or ""),
        )

    def __str__(self) -> str:
        return f"{self.issuer}/{self.namespace}/{self.kind}/{self.id}"

    @classmethod
    def parse(cls, text: str) -> "ResourceRef":
        parts = (text or "").split("/")
        if len(parts) != 4:
            raise ValueError(f"malformed ResourceRef: {text!r}")
        return cls(*parts)


def local_ref(kind: str, id: str) -> ResourceRef:
    return ResourceRef(LOCAL_ISSUER, LOCAL_NAMESPACE, kind, id)


def builtin_ref(kind: str, id: str) -> ResourceRef:
    return ResourceRef(BUILTIN_ISSUER, BUILTIN_NAMESPACE, kind, id)


def canonical_cloud_base(cloud_base: str) -> str:
    """Canonical origin and case-sensitive tenant prefix, without credentials."""
    base = (cloud_base or "").strip()
    if not base or "\\" in base or any(char.isspace() or ord(char) < 32 for char in base):
        raise ValueError("invalid cloud base")
    try:
        parsed = urlsplit(base)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("invalid cloud base")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid cloud base")
        host = parsed.hostname.lower()
        host = "[" + host + "]" if ":" in host else host.encode("idna").decode("ascii")
        port = parsed.port
        if port is not None and port != {"http": 80, "https": 443}[scheme]:
            host += ":" + str(port)
        return urlunsplit((scheme, host, parsed.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise ValueError("invalid cloud base") from None


def cloud_issuer(cloud_base: str) -> str:
    """Safe ResourceRef label bound to origin + tenant, including HTTP scheme."""
    canonical = canonical_cloud_base(cloud_base)
    return "cloud_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def cloud_ref(cloud_base: str, kind: str, id: str, *, scope: str) -> ResourceRef:
    if scope not in ("shared", "private"):
        raise ValueError(f"unknown scope {scope!r}")
    return ResourceRef(cloud_issuer(cloud_base), scope, kind, id)


def profile_id(cloud_base: str, subject: str) -> str:
    """Opaque, stable, collision-resistant profile id for (cloud instance, account)."""
    if not isinstance(subject, str) or not subject or "\0" in subject:
        raise ValueError("profile subject is empty or invalid")
    digest = hashlib.sha256(
        f"{canonical_cloud_base(cloud_base)}\0{subject}".encode("utf-8")
    ).hexdigest()
    # Wider v2 IDs cannot alias the old host-only 40-bit profiles. Old profile
    # bytes remain untouched and are never automatically merged or adopted.
    return f"p_{digest[:32]}"


def is_local_profile(profile: str) -> bool:
    return profile == LOCAL_PROFILE
