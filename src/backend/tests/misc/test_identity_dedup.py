"""同一个用户中心身份只能有一行影子用户：并发抢建的防护 + 存量重复行的合并。

历史上 ``users_shadow.user_center_id`` 只有普通索引，取用户又是"先查后建"。桌面壳
登录后一次性打出的那批桥接请求会同时插入，同一个人留下好几行；会话挂在 ``user_id``
上，认到哪一行就只看得见哪一行的会话——看起来就是"历史被清空了"。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.identity_dedup import merge_duplicate_user_shadows
from core.db.models import ChatSession, UserShadow
from core.db.repository import UserRepository
from core.auth.users import UserService

CENTER_ID = "cloud:example.test:443:42"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_shadow(db, user_id: str, created_at: datetime, center_id: str = CENTER_ID):
    db.add(
        UserShadow(
            user_id=user_id,
            username="tester",
            user_center_id=center_id,
            created_at=created_at,
        )
    )
    db.commit()


def _add_shadow_bypassing_constraint(db, user_id: str, created_at: datetime):
    """绕过唯一索引写入重复行，模拟约束上线前的存量数据。"""
    db.execute(text("DROP INDEX IF EXISTS uq_users_shadow_user_center_id"))
    db.commit()
    _add_shadow(db, user_id, created_at)


def test_unique_index_stops_a_second_row_for_the_same_identity(db):
    _add_shadow(db, "user_first", datetime(2026, 1, 1, 0, 0, 0))
    with pytest.raises(IntegrityError):
        _add_shadow(db, "user_second", datetime(2026, 1, 1, 0, 0, 1))
    db.rollback()


def test_blank_center_ids_do_not_collide(db):
    """未绑定身份的行（NULL / 空串）彼此不冲突——局部索引不管它们。"""
    db.add(UserShadow(user_id="user_a", username="a", user_center_id=None))
    db.add(UserShadow(user_id="user_b", username="b", user_center_id=None))
    db.add(UserShadow(user_id="user_c", username="c", user_center_id=""))
    db.add(UserShadow(user_id="user_d", username="d", user_center_id=""))
    db.commit()
    assert db.query(UserShadow).count() == 4


def test_lookup_always_returns_the_earliest_row(db):
    """存量重复行还没合并时，查询也必须稳定落在同一行（最早建的那一行）。"""
    base = datetime(2026, 1, 1, 0, 0, 0)
    _add_shadow(db, "user_zzz_earliest", base)
    _add_shadow_bypassing_constraint(db, "user_aaa_later", base + timedelta(seconds=1))

    repo = UserRepository(db)
    assert repo.get_by_user_center_id(CENTER_ID).user_id == "user_zzz_earliest"


def test_racing_insert_reuses_the_winning_row(db, monkeypatch):
    """并发里输掉的那一方不再留下第二行，而是复用赢家。"""
    _add_shadow(db, "user_winner", datetime(2026, 1, 1, 0, 0, 0))

    service = UserService(db)
    calls = {"n": 0}
    real_lookup = service.repo.get_by_user_center_id

    def lookup_missing_once(center_id: str):
        calls["n"] += 1
        # 第一次假装查不到——正是并发下另一行还没提交时看到的景象。
        return None if calls["n"] == 1 else real_lookup(center_id)

    monkeypatch.setattr(service.repo, "get_by_user_center_id", lookup_missing_once)

    user = service.get_or_create_user_shadow(user_center_id=CENTER_ID, username="tester")
    assert user.user_id == "user_winner"
    assert db.query(UserShadow).filter(UserShadow.user_center_id == CENTER_ID).count() == 1


def test_merge_keeps_earliest_row_and_repoints_its_chats(db, tmp_path):
    base = datetime(2026, 1, 1, 0, 0, 0)
    _add_shadow(db, "user_keep", base)
    _add_shadow_bypassing_constraint(db, "user_dup1", base + timedelta(milliseconds=1))
    _add_shadow_bypassing_constraint(db, "user_dup2", base + timedelta(milliseconds=2))
    db.add(ChatSession(chat_id="chat_on_dup", user_id="user_dup1", title="本机会话"))
    db.add(ChatSession(chat_id="chat_on_keep", user_id="user_keep", title="另一条"))
    db.commit()

    backup_dir = tmp_path / "backups"
    report = merge_duplicate_user_shadows(db.get_bind(), backup_dir=backup_dir)
    db.expire_all()

    assert report["groups"] == 1
    assert report["removed_user_ids"] == ["user_dup1", "user_dup2"]
    assert report["repointed"]["chat_sessions"] == 1

    remaining = db.query(UserShadow).filter(UserShadow.user_center_id == CENTER_ID).all()
    assert [row.user_id for row in remaining] == ["user_keep"]
    owners = {row.chat_id: row.user_id for row in db.query(ChatSession).all()}
    assert owners == {"chat_on_dup": "user_keep", "chat_on_keep": "user_keep"}

    backups = list(backup_dir.glob("users_shadow_merge_*.json"))
    assert len(backups) == 1
    payload = json.loads(backups[0].read_text(encoding="utf-8"))
    removed = payload["removed_rows"]["users_shadow"]
    assert {row["user_id"] for row in removed} == {"user_dup1", "user_dup2"}


def test_merge_is_a_no_op_without_duplicates(db, tmp_path):
    _add_shadow(db, "user_only", datetime(2026, 1, 1, 0, 0, 0))
    report = merge_duplicate_user_shadows(db.get_bind(), backup_dir=tmp_path / "backups")
    assert report["groups"] == 0
    assert report["removed_user_ids"] == []
    assert not (tmp_path / "backups").exists()


def test_merge_rolls_back_when_the_backup_cannot_be_written(db, tmp_path):
    """备份写不下去就整体回滚——宁可留着重复，也不无备份地删用户行。"""
    base = datetime(2026, 1, 1, 0, 0, 0)
    _add_shadow(db, "user_keep", base)
    _add_shadow_bypassing_constraint(db, "user_dup", base + timedelta(milliseconds=1))

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")  # mkdir 会失败

    with pytest.raises(OSError):
        merge_duplicate_user_shadows(db.get_bind(), backup_dir=blocked / "backups")
    db.expire_all()

    assert db.query(UserShadow).filter(UserShadow.user_center_id == CENTER_ID).count() == 2


def test_merge_drops_rows_that_would_collide_on_a_per_user_table(db, tmp_path):
    """每用户一行的表（如本地账号）改指会撞唯一键：保留行那份才是在用的。"""
    from core.db.models import LocalUser

    base = datetime(2026, 1, 1, 0, 0, 0)
    _add_shadow(db, "user_keep", base)
    _add_shadow_bypassing_constraint(db, "user_dup", base + timedelta(milliseconds=1))
    db.add(LocalUser(user_id="user_keep", password_hash="keep"))
    db.add(LocalUser(user_id="user_dup", password_hash="dup"))
    db.commit()

    report = merge_duplicate_user_shadows(db.get_bind(), backup_dir=tmp_path / "backups")
    db.expire_all()

    assert report["dropped"]["local_users"] == 1
    rows = db.query(LocalUser).all()
    assert [(row.user_id, row.password_hash) for row in rows] == [("user_keep", "keep")]
    # 被删的那一行必须在备份里，不只是 users_shadow。
    payload = json.loads(Path(report["backup_path"]).read_text(encoding="utf-8"))
    assert payload["removed_rows"]["local_users"][0]["password_hash"] == "dup"


def test_merge_skips_scanning_once_the_unique_index_is_in_place(db, tmp_path, monkeypatch):
    """约束在位之后重复不可能再出现——稳态下连分组扫描都不做。"""
    import core.db.identity_dedup as dedup

    _add_shadow(db, "user_only", datetime(2026, 1, 1, 0, 0, 0))

    def explode(_connection):
        raise AssertionError("must not scan for duplicates once the index exists")

    monkeypatch.setattr(dedup, "_duplicate_groups", explode)
    report = dedup.merge_duplicate_user_shadows(db.get_bind(), backup_dir=tmp_path / "backups")
    assert report["groups"] == 0
