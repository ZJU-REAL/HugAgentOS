"""站点会话的作业规则必须走系统提示，不许进用户消息。

背景（线上实测）：建站会话里用户随便说一句"做个公司官网"，刷新页面后自己的气泡里
多出一整段"[系统提示：这是「站点建站」会话。请在沙箱工作目录里生成完整的静态网站……]"。
原因是前端把这段面向模型的规则拼在消息尾部一起上行，后端按原样落库
（``chat_messages.content`` 存的就是发上来的那串），历史回放自然把它显示出来。

规则现在由 ``agent_factory._site_mode_hint`` 在系统提示里注入，开关是会话上的
``site_chat``。这里锁三条不变量：规则文本是系统提示段（不是内嵌在消息里的方括号块）、
建站与编辑两种会话给的是两套规则、以及 request → context → factory 这条开关链没断。
"""

import inspect
from pathlib import Path

from api.routes.v1 import chats as chats_route
from api.schemas import ChatRequest
from core.llm.agent_factory import _site_mode_hint, create_agent_executor


def test_site_rules_are_a_system_prompt_section() -> None:
    build = _site_mode_hint(None)
    assert build.startswith("\n\n## ")
    assert "publish_site" in build and "index.html" in build
    # 老写法的痕迹：一旦又出现在规则文本里，说明有人把它拼回消息了
    assert "[系统提示" not in build


def test_edit_session_rules_point_at_the_bound_project_folder() -> None:
    edit = _site_mode_hint({"project_id": "p1", "project_folder_name": "公司官网"})
    assert "/myspace/公司官网/" in edit
    assert "不要在其他目录重新生成整站" in edit
    # 逻辑路径，不能写沙箱里的物理路径
    assert "/workspace/myspace" not in edit
    assert edit != _site_mode_hint(None)
    # 挂了项目但文件夹名一时取不到，也仍然是编辑规则，不能退回"重新生成整站"
    assert "不要在其他目录重新生成整站" in _site_mode_hint({"project_id": "p1"})


def test_site_switch_reaches_the_agent_factory(monkeypatch) -> None:
    monkeypatch.setattr(chats_route, "_collect_historical_attachments", lambda **_kwargs: [])
    request = ChatRequest(chat_id="chat-1", message="做个公司官网", site_chat=True)
    ctx = chats_route._build_ctx(request, "user-1", None, None, None)

    assert ctx["site_chat"] is True
    assert "site_mode" in inspect.signature(create_agent_executor).parameters

    workflow_src = Path(__file__).resolve().parents[2].joinpath("orchestration/workflow.py")
    assert 'site_mode=bool(context.get("site_chat", False)),' in workflow_src.read_text()


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
