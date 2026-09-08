"""Capability failures give actionable feedback without exposing raw metadata."""

import pytest

from core.capabilities.dependency import DependencyMissing
from core.capabilities.errors import (
    CloudUnavailable,
    IntegrityFailed,
    NameConflict,
    PackageMissing,
    PermissionDenied,
    ViewUnavailable,
)
from core.chat.context import resolve_user_facing_error


@pytest.mark.parametrize(
    "error, hint",
    [
        (DependencyMissing, "必要依赖"),
        (PackageMissing, "准备"),
        (NameConflict, "同名冲突"),
        (ViewUnavailable, "占用"),
        (IntegrityFailed, "完整性"),
        (CloudUnavailable, "连接"),
        (PermissionDenied, "授权"),
    ],
)
def test_capability_failure_has_safe_recovery_hint(error, hint):
    canary = "CONFIDENTIAL_PROVIDER_TOKEN_123"
    result = resolve_user_facing_error(
        error(canary, details={"dependencies": [{"id": canary}], "path": canary})
    )
    assert hint in result
    assert "本轮已停止" in result
    assert canary not in result


def test_unrelated_errors_keep_existing_safe_mapping():
    assert resolve_user_facing_error(RuntimeError("timeout token=secret")) == "请求超时，请稍后重试"
