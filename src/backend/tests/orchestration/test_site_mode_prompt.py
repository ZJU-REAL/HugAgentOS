"""Site workflows belong to the site-builder skill, never user messages or ambient prompts."""

import inspect
from pathlib import Path

from api.routes.v1 import chats as chats_route
from api.schemas import ChatRequest
from core.llm.agent_factory import create_agent_executor


def test_site_flag_is_metadata_not_an_agent_prompt_switch(monkeypatch) -> None:
    monkeypatch.setattr(chats_route, "_collect_historical_attachments", lambda **_kwargs: [])
    request = ChatRequest(chat_id="chat-1", message="做个公司官网", site_chat=True)
    ctx = chats_route._build_ctx(request, "user-1", None, None, None)

    assert ctx["site_chat"] is True
    assert "site_mode" not in inspect.signature(create_agent_executor).parameters

    workflow_src = Path(__file__).resolve().parents[2].joinpath("orchestration/workflow.py")
    assert "site_mode=" not in workflow_src.read_text()


def test_user_message_carries_no_site_rules() -> None:
    """用户消息按原样落库、原样回放——组装用户消息的那一层不许往里塞规则。"""
    typed = "做个公司官网"
    assert chats_route._build_effective_user_message(typed, None) == typed


def test_history_repair_keeps_only_what_the_user_typed() -> None:
    """老会话里已经落库的规则要被清掉，用户自己打的字一个也不能少。"""
    from core.db.data_repair import strip_site_mode_hint_from_user_messages
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE chat_messages (message_id TEXT, role TEXT, content TEXT)"))
        rows = [
            (
                "m1",
                "user",
                "做个公司官网\n\n[系统提示：这是「站点建站」会话。请在沙箱工作目录里生成完整的静态网站]",
            ),
            (
                "m2",
                "user",
                "改一下首页配色\n\n[系统提示：这是「站点编辑」会话。该站点的全部源码已在项目文件夹中]",
            ),
            ("m3", "user", "普通消息，不该被动"),
            (
                "m4",
                "assistant",
                "助手消息里出现同样的字样也不动：[系统提示：这是「站点建站」会话。]",
            ),
        ]
        for row in rows:
            conn.execute(
                text(
                    "INSERT INTO chat_messages (message_id, role, content) " "VALUES (:a, :b, :c)"
                ),
                dict(zip("abc", row)),
            )

        assert strip_site_mode_hint_from_user_messages(conn) == 2
        stored = dict(
            conn.execute(text("SELECT message_id, content FROM chat_messages")).fetchall()
        )

    assert stored["m1"] == "做个公司官网"
    assert stored["m2"] == "改一下首页配色"
    assert stored["m3"] == "普通消息，不该被动"
    assert "[系统提示" in stored["m4"]
