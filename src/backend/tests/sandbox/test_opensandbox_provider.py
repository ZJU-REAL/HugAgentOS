"""Behavioral regression coverage."""

from __future__ import annotations
import asyncio
from unittest.mock import MagicMock
import pytest

from tests.sandbox.provider_test_support import _import_opensandbox_module

pytestmark = pytest.mark.usefixtures("provider_database")


def test_renew_in_background_marks_stale_on_lifecycle_error():
    """Background renew that hits a lifecycle error (404/terminated) marks the
    session for recreate-on-next-acquire; does NOT destroy mid-flight."""
    osp = _import_opensandbox_module()
    p = osp.OpenSandboxProvider.__new__(osp.OpenSandboxProvider)

    sess = osp._Session(
        sandbox=MagicMock(id="zzz"),
        interpreter=MagicMock(),
        contexts={},
        seeded_myspace_mtime={},
    )

    async def fake_renew(s):
        raise RuntimeError("sandbox not found: 404")

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
        interpreter=MagicMock(),
        contexts={},
        seeded_myspace_mtime={},
    )

    async def fake_renew(s):
        raise RuntimeError("ReadTimeout while reaching server")

    p._renew = fake_renew
    asyncio.run(p._renew_in_background("chat-x", sess))
    assert sess.stale_marked is False


def test_reset_language_context_drops_only_target_language():
    """No Jupyter kernel: ``_reset_language_context`` just pops the target
    language from ``sess.contexts`` (other languages untouched) and never talks
    to any SDK — execd commands.run is stateless, nothing to delete server-side.
    """
    osp = _import_opensandbox_module()
    p = osp.OpenSandboxProvider.__new__(osp.OpenSandboxProvider)

    delete_calls: list[str] = []

    async def fake_delete(ctx_id):
        delete_calls.append(ctx_id)

    interp = MagicMock()
    interp.codes = MagicMock()
    interp.codes.delete_context = fake_delete

    sess = osp._Session(
        sandbox=MagicMock(id="sbx-1"),
        interpreter=interp,
        contexts={
            "bash": MagicMock(id="ctx-bash-1"),
            "python": MagicMock(id="ctx-python-1"),
        },
        seeded_myspace_mtime={},
    )

    asyncio.run(p._reset_language_context(sess, "bash"))

    # bash dropped, python untouched; no SDK delete_context fired (kernel gone)
    assert "bash" not in sess.contexts
    assert "python" in sess.contexts
    assert delete_calls == []


def test_reset_language_context_never_touches_sdk():
    """Even if the SDK delete_context would raise, the reset must not call it —
    the path is now SDK-free, so a booby-trapped interpreter is never invoked.
    """
    osp = _import_opensandbox_module()
    p = osp.OpenSandboxProvider.__new__(osp.OpenSandboxProvider)

    async def boom(ctx_id):
        raise RuntimeError("should never be called")

    interp = MagicMock()
    interp.codes = MagicMock()
    interp.codes.delete_context = boom

    sess = osp._Session(
        sandbox=MagicMock(id="sbx-1"),
        interpreter=interp,
        contexts={"bash": MagicMock(id="ctx-bash-1")},
        seeded_myspace_mtime={},
    )

    # No exception leaks out (boom is never reached)
    asyncio.run(p._reset_language_context(sess, "bash"))
    assert "bash" not in sess.contexts


def test_reset_language_context_noop_when_ctx_missing():
    """Idempotent — calling reset for a language with no local handle must not
    blow up and must not talk to the SDK.
    """
    osp = _import_opensandbox_module()
    p = osp.OpenSandboxProvider.__new__(osp.OpenSandboxProvider)

    delete_calls: list[str] = []

    async def fake_delete(ctx_id):
        delete_calls.append(ctx_id)

    interp = MagicMock()
    interp.codes = MagicMock()
    interp.codes.delete_context = fake_delete

    sess = osp._Session(
        sandbox=MagicMock(id="sbx-1"),
        interpreter=interp,
        contexts={},  # already empty
        seeded_myspace_mtime={},
    )

    asyncio.run(p._reset_language_context(sess, "bash"))
    assert delete_calls == []  # never called the SDK
