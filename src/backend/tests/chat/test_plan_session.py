from api.routes.v1 import plans as plans_route
from core.chat.context import generate_smart_title


def test_plan_session_uses_task_based_initial_title(monkeypatch):
    captured = {}

    class StubChatService:
        def __init__(self, db):
            self.db = db

        def ensure_session(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(plans_route, "ChatService", StubChatService)

    task = "梳理本季度重点项目并输出风险清单"
    chat_id = plans_route._ensure_plan_session(
        object(),
        "chat_plan_title",
        "user_1",
        task,
    )

    assert chat_id == "chat_plan_title"
    assert captured["title"] == generate_smart_title(task)
    assert captured["title"] != "计划模式"
    assert captured["extra_data"] == {"plan_chat": True}
