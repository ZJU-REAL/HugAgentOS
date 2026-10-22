"""Behavioral regression coverage."""

from __future__ import annotations
import asyncio
from unittest.mock import MagicMock
import pytest

from core.sandbox.errors import SandboxNotFoundError

from tests.sandbox.provider_test_support import _import_opensandbox_module

pytestmark = pytest.mark.usefixtures("provider_database")


def test_renew_in_background_marks_stale_on_lifecycle_error():
    """Background renew that confirms a missing sandbox marks the
    session for recreate-on-next-acquire; does NOT destroy mid-flight."""
    osp = _import_opensandbox_module()
    p = osp.OpenSandboxProvider.__new__(osp.OpenSandboxProvider)

    sess = osp._Session(
        sandbox=MagicMock(id="zzz"),
    )

    async def fake_renew(s):
        raise SandboxNotFoundError("sandbox no longer exists")

    p._renew = fake_renew  # bound replacement
    asyncio.run(p._renew_in_background("chat-x", sess))
    assert sess.stale_marked is True


def test_renew_in_background_keeps_session_on_transient_error():
    """A transient renew error (e.g. ReadTimeout) MUST NOT mark stale; the
    session stays usable, retry happens next interval."""
    osp = _import_opensandbox_module()
    p = osp.OpenSandboxProvider.__new__(osp.OpenSandboxProvider)
    sess = osp._Session(
        sandbox=MagicMock(id="zzz"),
    )

    async def fake_renew(s):
        raise RuntimeError("ReadTimeout while reaching server")

    p._renew = fake_renew
    asyncio.run(p._renew_in_background("chat-x", sess))
    assert sess.stale_marked is False
