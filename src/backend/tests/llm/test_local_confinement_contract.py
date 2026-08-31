"""The OS-confinement contract belongs to the permission preset.

``wrap_command`` only reports whether a backend exists; what to do about a
missing one is a policy decision. Getting this wrong in either direction is
serious: refusing everywhere leaves the Windows desktop client with no shell,
while degrading everywhere silently drops the write jail.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from core.llm.tool_permissions import (
    APPROVAL_ASK,
    APPROVAL_AUTO,
    APPROVAL_FULL,
    CONFINE_NONE,
    CONFINE_PREFERRED,
    CONFINE_REQUIRED,
    FAIL_CLOSED_MODE,
    LocalCommandAuthorization,
    LocalConfinementUnavailableError,
)

_UNAVAILABLE = "core.sandbox.os_sandbox.confinement_unavailable_reason"


def _authorization(mode: str) -> LocalCommandAuthorization:
    return LocalCommandAuthorization(
        command="ls -la",
        approval_mode=mode,
        write_paths=("/workspace",),
    )


@pytest.mark.parametrize(
    "mode,expected",
    [
        (APPROVAL_ASK, CONFINE_PREFERRED),
        (APPROVAL_AUTO, CONFINE_PREFERRED),
        (APPROVAL_FULL, CONFINE_NONE),
        (FAIL_CLOSED_MODE, CONFINE_REQUIRED),
        ("something-new", CONFINE_REQUIRED),
    ],
)
def test_each_preset_declares_its_confinement_contract(mode, expected):
    """An unrecognised preset gets the most restrictive contract, not the loosest."""
    assert _authorization(mode).confinement == expected


def test_fail_closed_refuses_to_run_without_a_confinement_backend():
    """本机安全配置读不出来时的记号档，宁可没有 shell 也不裸跑。"""
    with patch(_UNAVAILABLE, return_value="当前平台 windows 尚无可用的文件系统隔离后端"):
        with pytest.raises(LocalConfinementUnavailableError) as excinfo:
            _authorization(FAIL_CLOSED_MODE).confine("ls -la")

    assert FAIL_CLOSED_MODE in str(excinfo.value)


@pytest.mark.parametrize("mode", [APPROVAL_ASK, APPROVAL_AUTO])
def test_asking_presets_degrade_with_a_warning_instead_of_killing_the_shell(mode):
    """Windows and bwrap-less Linux still get a shell, governed by the policy gate."""
    with patch(_UNAVAILABLE, return_value="当前平台 windows 尚无可用的文件系统隔离后端"):
        result = _authorization(mode).confine("ls -la")

    assert result.command == "ls -la"
    assert result.confined is False
    assert result.warning


def test_full_is_unconfined_without_any_warning_noise():
    with patch(_UNAVAILABLE, return_value=""):
        result = _authorization(APPROVAL_FULL).confine("ls -la")

    assert result.command == "ls -la"
    assert result.confined is False
    assert result.warning == ""


def test_an_available_backend_actually_wraps_the_command():
    with (
        patch(_UNAVAILABLE, return_value=""),
        patch("core.sandbox.os_sandbox.wrap_command", return_value="bwrap ... ls -la") as wrap,
    ):
        result = _authorization(APPROVAL_ASK).confine("ls -la")

    assert result.confined is True
    assert result.command == "bwrap ... ls -la"
    wrap.assert_called_once()
