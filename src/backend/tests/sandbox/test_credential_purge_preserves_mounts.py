"""Regression tests: clearing a user's credentials must never unlink the
directories that are bind-mounted into their live sandboxes.

``lark_cache/{uid}``, ``dws_cache/{uid}`` and ``email_cache/{uid}`` each hold
subdirectories that the OpenSandbox volume builders bind into the user's
running sandboxes. A bind mount is bound to the *inode*, so ``rmtree`` on one
of those trees orphans every live mount: the sandbox sees an empty directory
forever and credentials written by a later re-login land on a new inode it can
never see. Observed in production as "扫码登录成功了，会话里仍报 not configured".
The fix empties the trees in place; these tests pin that behaviour.
"""

from __future__ import annotations
from integrations.lark import process as lark_process
from integrations.lark import service as lark_service
import integrations.email.storage as integrations_email_storage
import integrations.lark.storage as integrations_lark_storage
import integrations.dingtalk.storage as integrations_dingtalk_storage

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.db.models  # noqa: F401  registers all models (FKs depend on users_shadow)
import core.sandbox.workspace.credentials as common_credentials
from core.db.engine import Base
from core.db.models import UserShadow
from core.sandbox.workspace.credentials import purge_credential_dir

# The bind-mount subpaths the volume builders declare, per platform. These are
# the exact directories whose inode must survive a purge.
_MOUNT_SUBPATHS = (
    "home/.lark-cli",
    "home/.local/share/lark-cli",
    "home/.dws",
    "home/.local/share/dws-cli",
    "home/.config/himalaya",
)


@pytest.fixture
def db(monkeypatch):
    eng = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    monkeypatch.setattr("core.db.engine.SessionLocal", sessionmaker(bind=eng))
    s = sessionmaker(bind=eng)()
    s.add(UserShadow(user_id="u1", username="alice"))
    s.commit()
    yield s
    s.close()


def _seed_tree(root: Path) -> dict[str, int]:
    """Build a credential tree like a real login leaves behind; return the
    inode of every mount subpath so the test can prove it was preserved."""
    inodes: dict[str, int] = {}
    for sub in _MOUNT_SUBPATHS:
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text("{}", encoding="utf-8")
        (d / "master.key").write_bytes(b"\x00" * 32)
        inodes[sub] = d.stat().st_ino
    # A nested subdirectory with content, as lark-cli's cache/ and logs/ produce
    nested = root / "home/.lark-cli/logs"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "auth.log").write_text("x", encoding="utf-8")
    return inodes


def _assert_emptied_but_mounted(root: Path, inodes: dict[str, int]) -> None:
    leftover = [str(p) for p in root.rglob("*") if p.is_file()]
    assert leftover == [], f"credential files survived the purge: {leftover}"
    for sub, ino in inodes.items():
        d = root / sub
        assert d.is_dir(), f"mount source {sub} was unlinked — live sandboxes would break"
        assert d.stat().st_ino == ino, f"mount source {sub} was recreated as a new inode"


# ── The helper itself ────────────────────────────────────────────────────


def test_purge_removes_files_and_keeps_every_mount_inode(tmp_path):
    root = tmp_path / "lark_cache" / "u1"
    inodes = _seed_tree(root)

    purge_credential_dir(root)

    _assert_emptied_but_mounted(root, inodes)


def test_purge_is_a_noop_when_the_tree_does_not_exist(tmp_path):
    purge_credential_dir(tmp_path / "never_created")  # must not raise


def test_purge_keeps_the_root_itself(tmp_path):
    root = tmp_path / "dws_cache" / "u1"
    root.mkdir(parents=True)
    ino = root.stat().st_ino

    purge_credential_dir(root)

    assert root.is_dir() and root.stat().st_ino == ino


# ── The three services that clear credentials ────────────────────────────


def test_email_purge_config_preserves_the_himalaya_mount(db, monkeypatch, tmp_path):
    """The easiest path to trigger: this runs on **every failed mailbox
    verification**, not just on disconnect."""
    from integrations.email.service import EmailService

    root = tmp_path / "email_cache" / "u1"
    inodes = _seed_tree(root)
    monkeypatch.setattr(integrations_email_storage, 'email_cache_dir', lambda uid: tmp_path / "email_cache" / uid)

    EmailService()._purge_config("u1")

    _assert_emptied_but_mounted(root, inodes)


def test_lark_disconnect_preserves_the_lark_cli_mounts(db, monkeypatch, tmp_path):
    from integrations.lark import service as ls

    root = tmp_path / "lark_cache" / "u1"
    inodes = _seed_tree(root)
    monkeypatch.setattr(integrations_lark_storage, 'lark_cache_dir', lambda uid: tmp_path / "lark_cache" / uid)

    async def _fake_run(*_a, **_kw):
        return "", "", 0

    monkeypatch.setattr(lark_process, 'run_lark', _fake_run)

    svc = lark_service.LarkService()
    asyncio.run(svc.repo.ensure("u1"))
    result = asyncio.run(svc.disconnect("u1"))

    assert result["status"] == "disconnected"
    _assert_emptied_but_mounted(root, inodes)


def test_dingtalk_disconnect_preserves_the_dws_mounts(db, monkeypatch, tmp_path):
    from integrations.dingtalk import service as ds

    root = tmp_path / "dws_cache" / "u1"
    inodes = _seed_tree(root)
    monkeypatch.setattr(integrations_dingtalk_storage, 'dws_cache_dir', lambda uid: tmp_path / "dws_cache" / uid)

    async def _fake_run(*_a, **_kw):
        return "", "", 0

    monkeypatch.setattr(ds, "_run_dws", _fake_run)

    svc = ds.DingTalkService()
    asyncio.run(svc.repo.ensure("u1"))
    result = asyncio.run(svc.disconnect("u1"))

    assert result["status"] == "disconnected"
    _assert_emptied_but_mounted(root, inodes)
