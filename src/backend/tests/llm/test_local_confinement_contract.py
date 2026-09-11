"""The confinement decision belongs to the user's permission preset.

There are exactly two outcomes and no third: the preset the user picked either
waives confinement, or the command runs under the OS sandbox. "The host has no
backend, so run it anyway" is not one of them — that is the failure mode this
module exists to keep out.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from core.llm.tool_permissions import (
    APPROVAL_ASK,
    APPROVAL_AUTO,
    APPROVAL_FULL,
    FAIL_CLOSED_MODE,
    UNCONFINED_APPROVAL_MODES,
    LocalCommandAuthorization,
    LocalConfinementUnavailableError,
)
from core.sandbox.os_sandbox import LocalAccessDecision
from core.sandbox.oslayer import SandboxUnavailableError, SandboxUnenforceableError

_CONFINE = "core.sandbox.os_sandbox.confine"


def _authorization(mode: str, workspace: str) -> LocalCommandAuthorization:
    return LocalCommandAuthorization(
        command="ls -la",
        approval_mode=mode,
        workspace_root=workspace,
        access=LocalAccessDecision(
            approval_mode=mode,
            unconfined=mode in UNCONFINED_APPROVAL_MODES,
            writable_roots=(workspace,),
        ),
    )


@pytest.mark.parametrize(
    "mode,confined",
    [
        (APPROVAL_ASK, True),
        (APPROVAL_AUTO, True),
        (APPROVAL_FULL, False),
        (FAIL_CLOSED_MODE, True),
        ("something-new", True),
    ],
)
def test_only_the_unrestricted_preset_waives_confinement(mode, confined, tmp_path):
    """An unrecognised preset is confined, never treated as the loosest one."""
    assert _authorization(mode, str(tmp_path)).confined is confined


def test_the_unrestricted_preset_runs_without_a_launch(tmp_path):
    """`full` means "run it as me"; honouring that is the setting, not a fallback."""
    with patch(_CONFINE) as confine:
        assert _authorization(APPROVAL_FULL, str(tmp_path)).confine() is None
    confine.assert_not_called()


@pytest.mark.parametrize("mode", [APPROVAL_ASK, APPROVAL_AUTO, FAIL_CLOSED_MODE])
def test_a_missing_backend_refuses_the_command(mode, tmp_path):
    reason = "本机缺少 Linux 沙箱运行器 bwrap（bubblewrap）"
    with patch(_CONFINE, side_effect=SandboxUnavailableError(reason)):
        with pytest.raises(LocalConfinementUnavailableError) as excinfo:
            _authorization(mode, str(tmp_path)).confine()

    message = str(excinfo.value)
    assert reason in message
    # The refusal has to tell the user which choice would let this run.
    assert "权限档" in message


def test_a_policy_the_platform_cannot_enforce_refuses_too(tmp_path):
    reason = "Windows 受限令牌沙箱无法限制读取范围，已拒绝执行"
    with patch(_CONFINE, side_effect=SandboxUnenforceableError(reason)):
        with pytest.raises(LocalConfinementUnavailableError) as excinfo:
            _authorization(APPROVAL_ASK, str(tmp_path)).confine()
    assert reason in str(excinfo.value)


def test_an_available_backend_produces_a_real_launch(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launch = _authorization(APPROVAL_ASK, str(workspace)).confine()

    assert launch is not None
    assert launch.backend
    assert launch.argv_prefix
