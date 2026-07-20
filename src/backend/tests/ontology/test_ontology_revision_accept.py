from __future__ import annotations

from types import SimpleNamespace

from core.services.chat_service import ChatService


class _MessageRepository:
    def __init__(self, message):
        self.message = message
        self.updated = None

    def get_by_id(self, message_id: str):
        return self.message if self.message.message_id == message_id else None

    def update(self, message_id: str, payload: dict):
        self.updated = payload
        self.message.content = payload["content"]
        self.message.extra_data = payload["extra_data"]
        return self.message


def test_accept_ontology_revision_replaces_content_and_marks_metadata():
    message = SimpleNamespace(
        message_id="msg_1",
        role="assistant",
        content="原始答案",
        extra_data={
            "ontology_governance": {"review": {"candidate_answer": "基于新增证据的优化答案"}}
        },
    )
    service = ChatService.__new__(ChatService)
    service.message_repo = _MessageRepository(message)

    updated = service.accept_ontology_revision("msg_1")

    assert updated.content == "基于新增证据的优化答案"
    review = updated.extra_data["ontology_governance"]["review"]
    assert review["accepted"] is True
    assert review["candidate_answer"] == "基于新增证据的优化答案"


def test_accept_ontology_revision_rejects_message_without_candidate():
    message = SimpleNamespace(
        message_id="msg_2",
        role="assistant",
        content="原始答案",
        extra_data={"ontology_governance": {"review": {}}},
    )
    service = ChatService.__new__(ChatService)
    service.message_repo = _MessageRepository(message)

    assert service.accept_ontology_revision("msg_2") is None


def test_accept_ontology_revision_rejects_placeholder_candidate():
    message = SimpleNamespace(
        message_id="msg_3",
        role="assistant",
        content="必须保留的原始答案",
        extra_data={"ontology_governance": {"review": {"candidate_answer": "..."}}},
    )
    service = ChatService.__new__(ChatService)
    service.message_repo = _MessageRepository(message)

    assert service.accept_ontology_revision("msg_3") is None
    assert message.content == "必须保留的原始答案"


def test_accept_ontology_revision_removes_transport_wrapper():
    message = SimpleNamespace(
        message_id="msg_4",
        role="assistant",
        content="原始答案",
        extra_data={
            "ontology_governance": {
                "review": {
                    "candidate_answer": (
                        "<ontology_revision>\n基于补充证据的完整优化答案\n"
                        "</ontology_revision>"
                    )
                }
            }
        },
    )
    service = ChatService.__new__(ChatService)
    service.message_repo = _MessageRepository(message)

    updated = service.accept_ontology_revision("msg_4")

    assert updated.content == "基于补充证据的完整优化答案"
    assert (
        updated.extra_data["ontology_governance"]["review"]["candidate_answer"]
        == "基于补充证据的完整优化答案"
    )
