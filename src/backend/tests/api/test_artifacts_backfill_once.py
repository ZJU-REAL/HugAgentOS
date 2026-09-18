"""历史文件回填只准跑一次，而且绝不能压在请求线程上。

这段回填要把账号写过的每一条消息都扫一遍。它原先直接跑在 ``async def`` 路由体内，于是
一次「我的空间」首开就把事件循环占住两分钟——期间整个后端一个请求都完成不了（生产实测
118s）。而且"已回填"只记在进程内存里，后端每重启一次就重来一遍。

因此有两条硬约束：
  1. 路由只把回填交给后台任务，自己不等它；
  2. 回填成功后把标记落到 ``users_shadow.metadata``，重启后不再重扫。
"""

import asyncio
from datetime import datetime

import pytest
from api.routes.v1 import artifacts as route
from core.auth.backend import UserContext
from core.db.models import Artifact, ChatMessage, ChatSession, UserShadow

USER_ID = "u-backfill"


@pytest.fixture
def user_with_history(db_session):
    route._backfilled_users.clear()
    route._backfilling_users.clear()
    db_session.add(UserShadow(user_id=USER_ID, username="backfill", extra_data={}))
    db_session.add(
        ChatSession(
            chat_id="chat-1", user_id=USER_ID, title="旧会话", created_at=datetime(2026, 1, 1)
        )
    )
    db_session.add(
        ChatMessage(
            message_id="msg-1",
            chat_id="chat-1",
            chat_seq=1,
            role="user",
            content="带附件的历史消息",
            extra_data={
                "attachments": [
                    {
                        "file_id": "hist-file-1",
                        "name": "旧附件.docx",
                        "mime_type": (
                            "application/vnd.openxmlformats-officedocument"
                            ".wordprocessingml.document"
                        ),
                        "size": 1024,
                        "url": "/files/hist-file-1",
                    }
                ]
            },
        )
    )
    db_session.commit()
    yield db_session
    route._backfilled_users.clear()
    route._backfilling_users.clear()


def test_scan_registers_historical_attachments(user_with_history):
    created = route._backfill_artifacts_from_messages(USER_ID, user_with_history)

    assert created == 1
    row = user_with_history.query(Artifact).filter_by(artifact_id="hist-file-1").one()
    assert row.user_id == USER_ID
    assert row.filename == "旧附件.docx"


def test_scan_is_idempotent(user_with_history):
    route._backfill_artifacts_from_messages(USER_ID, user_with_history)

    assert route._backfill_artifacts_from_messages(USER_ID, user_with_history) == 0


def test_marker_survives_a_restart(user_with_history, monkeypatch):
    monkeypatch.setattr(route, "SessionLocal", lambda: user_with_history)
    calls = []
    real_scan = route._backfill_artifacts_from_messages
    monkeypatch.setattr(
        route,
        "_backfill_artifacts_from_messages",
        lambda uid, db: calls.append(uid) or real_scan(uid, db),
    )

    route._run_backfill_once(USER_ID)
    shadow = user_with_history.query(UserShadow).filter_by(user_id=USER_ID).one()
    assert shadow.extra_data.get(route._BACKFILL_MARKER)

    # 重启：进程内缓存清空，但库里的标记还在，不该再扫一遍
    route._backfilled_users.clear()
    route._run_backfill_once(USER_ID)

    assert calls == [USER_ID]


def test_failed_scan_does_not_claim_the_marker(user_with_history, monkeypatch):
    monkeypatch.setattr(route, "SessionLocal", lambda: user_with_history)

    def _boom(uid, db):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(route, "_backfill_artifacts_from_messages", _boom)
    route._run_backfill_once(USER_ID)

    shadow = user_with_history.query(UserShadow).filter_by(user_id=USER_ID).one()
    assert not shadow.extra_data.get(route._BACKFILL_MARKER)


def test_listing_never_runs_the_scan_on_the_request(user_with_history, monkeypatch):
    """列表必须立刻返回：扫描既不能在事件循环上跑，也不能占用请求线程池。

    后者是仓库明文约定（见 core/kb/index_queue.py 开头的复盘）：``BackgroundTasks``
    的同步任务会吃掉 anyio 请求线程池的令牌，而每个请求都要经 ``get_db`` 拿一个。
    """
    monkeypatch.setattr(
        route,
        "_backfill_artifacts_from_messages",
        lambda uid, db: pytest.fail("回填不得跑在请求路径上"),
    )

    async def _call():
        await route.list_user_artifacts(
            type=None,
            source_kind=None,
            keyword=None,
            scope="personal",
            folder_id=None,
            page=1,
            page_size=50,
            user=UserContext(user_id=USER_ID, user_center_id=USER_ID, username="backfill"),
            db=user_with_history,
        )
        # 扫描被甩到线程里，请求这边不等它
        assert route._backfill_tasks
        for task in list(route._backfill_tasks):
            task.cancel()

    asyncio.run(_call())
