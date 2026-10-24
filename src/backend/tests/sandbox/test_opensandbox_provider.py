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

@pytest.mark.parametrize("domain,expected", [
    ("127.0.0.1:46074", "http://127.0.0.1:46074"),
    ("http://controller:8080/", "http://controller:8080"),
    ("https://controller.example/prefix/", "https://controller.example/prefix"),
])
def test_controller_url_shared_by_sdk_health_and_renew(monkeypatch, domain, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    osp = _import_opensandbox_module()
    original = osp.settings.sandbox
    class Config:
        opensandbox_domain = domain
        def __getattr__(self, key):
            return getattr(original, key)
    monkeypatch.setattr(osp, "settings", SimpleNamespace(sandbox=Config()))
    p = osp.OpenSandboxProvider()
    client = SimpleNamespace(
        post=AsyncMock(return_value=SimpleNamespace(status_code=200)),
        get=AsyncMock(return_value=SimpleNamespace(status_code=200)),
    )
    p._get_http_client = lambda: client
    p._session_expiry_s = lambda: 600
    async def check():
        await p._renew(osp._Session(sandbox=SimpleNamespace(id="recovered")))
        assert await p.health()
    asyncio.run(check())
    assert p._make_config().get_base_url() == expected + "/v1"
    assert client.post.call_args.args[0] == expected + "/v1/sandboxes/recovered/renew-expiration"
    assert client.get.call_args.args[0] == expected + "/health"
