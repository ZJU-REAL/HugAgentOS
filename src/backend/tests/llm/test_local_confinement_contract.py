"""The OS-confinement contract belongs to the permission preset.

``wrap_command`` only reports whether a backend exists; what to do about a
missing one is a policy decision. Getting this wrong in either direction is
serious: refusing everywhere leaves the Windows desktop client with no shell,
while degrading everywhere silently drops the write jail that ``strict``
exists to guarantee.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from core.llm.tool_permissions import (
    CONFINE_NONE,
    CONFINE_PREFERRED,
    CONFINE_REQUIRED,
    LocalCommandAuthorization,
    LocalConfinementUnavailableError,
)

_UNAVAILABLE = "core.sandbox.os_sandbox.confinement_unavailable_reason"


def _authorization(mode: str) -> LocalCommandAuthorization:
    return LocalCommandAuthorization(
        command="ls -la",
        approval_mode=mode,
        write_paths=("/workspace",),
        read_only=(mode == "strict"),
    )


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("strict", CONFINE_REQUIRED),
        ("standard", CONFINE_PREFERRED),
        ("full", CONFINE_NONE),
        ("something-new", CONFINE_REQUIRED),
    ],
)
def test_each_preset_declares_its_confinement_contract(mode, expected):
    """An unrecognised preset gets the most restrictive contract, not the loosest."""
    assert _authorization(mode).confinement == expected


def test_strict_refuses_to_run_without_a_confinement_backend():
    with patch(_UNAVAILABLE, return_value="当前平台 windows 尚无可用的文件系统隔离后端"):
        with pytest.raises(LocalConfinementUnavailableError) as excinfo:
            _authorization("strict").confine("ls -la")

    assert "strict" in str(excinfo.value)


def test_standard_degrades_with_a_warning_instead_of_killing_the_shell():
    """Windows and bwrap-less Linux still get a shell, governed by the policy gate."""
    with patch(_UNAVAILABLE, return_value="当前平台 windows 尚无可用的文件系统隔离后端"):
        result = _authorization("standard").confine("ls -la")

    assert result.command == "ls -la"
    assert result.confined is False
    assert result.warning


def test_full_is_unconfined_without_any_warning_noise():
    with patch(_UNAVAILABLE, return_value=""):
        result = _authorization("full").confine("ls -la")

    assert result.command == "ls -la"
    assert result.confined is False
    assert result.warning == ""


def test_an_available_backend_actually_wraps_the_command():
    with (
        patch(_UNAVAILABLE, return_value=""),
        patch("core.sandbox.os_sandbox.wrap_command", return_value="bwrap ... ls -la") as wrap,
    ):
        result = _authorization("standard").confine("ls -la")

    assert result.confined is True
    assert result.command == "bwrap ... ls -la"
    wrap.assert_called_once()


def test_strict_requests_a_read_only_jail():
    with (
        patch(_UNAVAILABLE, return_value=""),
        patch("core.sandbox.os_sandbox.wrap_command", return_value="jailed") as wrap,
    ):
        _authorization("strict").confine("ls -la")

    assert wrap.call_args.kwargs["read_only"] is True
