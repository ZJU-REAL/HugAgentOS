"""Structured capability errors surfaced to the API and the UI."""

from __future__ import annotations

from typing import Any, Dict, Optional


class CapabilityError(Exception):
    """Base error with a stable machine code and a recovery hint."""

    code = "capability_error"
    retryable = False
    recovery_action = ""

    def __init__(
        self,
        message: str,
        *,
        ref: Optional[str] = None,
        runtime_name: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.ref = ref
        self.runtime_name = runtime_name
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "resource_ref": self.ref,
            "runtime_name": self.runtime_name,
            "retryable": self.retryable,
            "recovery_action": self.recovery_action,
            **self.details,
        }


class PackageMissing(CapabilityError):
    code = "package_missing"
    retryable = True
    recovery_action = "prepare"


class NameConflict(CapabilityError):
    code = "name_conflict"
    recovery_action = "choose_name_preference"


class ViewUnavailable(CapabilityError):
    code = "view_unavailable"
    retryable = True
    recovery_action = "rebuild_view"


class IntegrityFailed(CapabilityError):
    code = "integrity_failed"
    recovery_action = "quarantine_and_retry"


class InstallConflict(CapabilityError):
    code = "install_conflict"
    recovery_action = "inspect_path"


class CloudUnavailable(CapabilityError):
    code = "cloud_unavailable"
    retryable = True
    recovery_action = "reconnect"


class PermissionDenied(CapabilityError):
    code = "permission_denied"
    recovery_action = "contact_admin"
