from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from core.harness.usage import AttemptUsage
from core.observability import langfuse as lf


class FakeObservation:
    def __init__(self, **started):
        self.started = started
        self.updates = []
        self.children = []
        self.ended = False

    def update(self, **kwargs):
        self.updates.append(kwargs)
        return self

    def end(self):
        self.ended = True
        return self

    def start_observation(self, **kwargs):
        child = FakeObservation(**kwargs)
        self.children.append(child)
        return child


class FakeClient:
    def __init__(self):
        self.roots = []
        self.scores = []
        self.propagated = []
        self.shutdown_called = False

    @staticmethod
    def create_trace_id(*, seed):
        return (seed.encode().hex() + "0" * 32)[:32]

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        root = FakeObservation(**kwargs)
        self.roots.append(root)
        try:
            yield root
        finally:
            root.end()

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def shutdown(self):
        self.shutdown_called = True


def _configure(monkeypatch, *, capture_content=True, capture_tool_io=False):
    client = FakeClient()

    @contextmanager
    def propagate(**kwargs):
        client.propagated.append(kwargs)
        yield

    fake_settings = SimpleNamespace(
        langfuse=SimpleNamespace(
            enabled=True,
            capture_content=capture_content,
            capture_tool_io=capture_tool_io,
            max_content_chars=8000,
            environment="staging",
            release="test-release",
        )
    )
    monkeypatch.setattr(lf, "settings", fake_settings)
    monkeypatch.setattr(lf, "_sdk", lambda: (client, propagate))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    return client


def test_chat_trace_nests_attempts_and_masks_content(monkeypatch):
    client = _configure(monkeypatch, capture_content=True, capture_tool_io=False)

    with lf.chat_trace_scope(
        run_id="run_123",
        chat_id="chat_123",
        user_id="user_123",
        message_id="msg_123",
        question="手机号 13800138000",
        model_name="model-a",
        recovering=False,
    ):
        child = lf.start_attempt_observation(
            kind="model",
            name="model-a",
            model="model-a",
            metadata={"provider": "fake"},
        )
        lf.finish_attempt_observation(
            child,
            status="success",
            usage=AttemptUsage(prompt_tokens=11, completion_tokens=7),
        )
        lf.finish_current_chat_trace(
            status="completed",
            answer="<think>private reasoning</think>最终答案",
            metadata={"tool_call_count": 2},
        )

    root = client.roots[0]
    question = root.started["input"]["question"]["content"]
    assert "13800138000" not in question
    assert root.children[0].ended is True
    assert root.children[0].updates[-1]["usage_details"]["input_tokens"] == 11
    answer = root.updates[-1]["output"]["answer"]["content"]
    assert answer == "最终答案"
    assert client.propagated[0]["session_id"] == "chat_123"
    assert root.ended is True


def test_metadata_only_capture_does_not_export_chat_text(monkeypatch):
    _configure(monkeypatch, capture_content=False)

    with lf.chat_trace_scope(
        run_id="run_456",
        chat_id="chat_456",
        user_id="user_456",
        message_id="msg_456",
        question="sensitive question",
        model_name=None,
        recovering=False,
    ) as state:
        assert state.root.started["input"]["question"] == {
            "captured": False,
            "characters": 18,
        }


def test_feedback_uses_deterministic_trace_and_score_ids(monkeypatch):
    client = _configure(monkeypatch, capture_content=True)

    lf.record_user_feedback(
        run_id="run_score",
        message_id="msg_score",
        rating="dislike",
        comment="手机号 13800138000",
    )
    lf.record_user_feedback(
        run_id="run_score",
        message_id="msg_score",
        rating="like",
        comment=None,
    )

    first, second = client.scores
    assert first["trace_id"] == second["trace_id"]
    assert first["score_id"] == second["score_id"]
    assert first["value"] == 0.0
    assert second["value"] == 1.0
    assert "13800138000" not in first["comment"]


def test_disabled_integration_is_noop(monkeypatch):
    fake_settings = SimpleNamespace(
        langfuse=SimpleNamespace(enabled=False, capture_content=False)
    )
    monkeypatch.setattr(lf, "settings", fake_settings)
    monkeypatch.setattr(
        lf,
        "_sdk",
        lambda: (_ for _ in ()).throw(AssertionError("SDK must not load")),
    )

    with lf.chat_trace_scope(
        run_id="run_off",
        chat_id="chat_off",
        user_id="user_off",
        message_id="msg_off",
        question="hello",
        model_name=None,
        recovering=False,
    ) as state:
        assert state is None
