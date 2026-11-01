"""A run's log scope (run_id) and the HTTP requests (trace_ids) that acted on it."""

from __future__ import annotations

import asyncio

import core.harness.request_links as request_links
import core.runs.runtime as runtime
import pytest
from core.db.engine import Base
from core.harness.ledger import DurableEventStore
from core.infra.logging import add_user_context, run_id_var, trace_id_var
from core.llm.runtime import interaction_store
from core.runs.journal import RunJournal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

RUN = "run-links"


@pytest.fixture()
def sessions(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'request-links.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    RunJournal(factory).accept(
        run_id=RUN, message_id="msg-links", chat_id="chat-links", user_id="user-links",
        request_payload={"kind": "chat"}, recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    monkeypatch.setattr(request_links, "DurableEventStore", lambda: DurableEventStore(factory))
    yield factory
    engine.dispose()


def _links(factory):
    with factory() as db:
        return [(link["action"], link["trace_id"]) for link in request_links.list_request_links(db, RUN)]


async def _settle_links():
    pending = [t for t in asyncio.all_tasks() if t.get_name().startswith("run_request_link:")]
    await asyncio.gather(*pending)


@pytest.mark.asyncio
async def test_run_task_logs_under_its_run_id_and_links_the_starting_request(sessions):
    seen = {}

    async def worker():
        seen["run_id"] = run_id_var.get()
        seen["request"] = request_links.current_request_trace()

    token = trace_id_var.set("trace-start")
    try:
        runtime.register_run_task(RUN, worker(), name=f"test:{RUN}")
        await runtime.tasks.process_state().active_runs[RUN]
    finally:
        trace_id_var.reset(token)
    await _settle_links()

    assert seen == {"run_id": RUN, "request": ""}  # inside the run, nothing counts as a request
    assert run_id_var.get() == ""  # the caller's context is untouched
    assert _links(sessions) == [("start", "trace-start")]


@pytest.mark.asyncio
async def test_run_started_without_a_request_links_nothing(sessions):
    async def worker():
        return None

    runtime.register_run_task(RUN, worker(), name=f"test:{RUN}")  # e.g. startup recovery
    await runtime.tasks.process_state().active_runs[RUN]
    await _settle_links()
    assert _links(sessions) == []


@pytest.mark.asyncio
async def test_answer_links_the_answering_request_to_the_run_that_asked(sessions):
    async def ask():
        await interaction_store.register("question", "chat-links", "q1", {"questions": []}, 60)

    run_token = run_id_var.set(RUN)
    try:
        await asyncio.create_task(ask())  # registered from inside the run's worker task
    finally:
        run_id_var.reset(run_token)

    trace_token = trace_id_var.set("trace-answer")
    try:
        assert await interaction_store.decide("question", "chat-links", "q1", {"outcome": "answered"})
    finally:
        trace_id_var.reset(trace_token)
        await interaction_store.remove("question", "chat-links", "q1")

    assert _links(sessions) == [("answer:question", "trace-answer")]


@pytest.mark.asyncio
async def test_actions_taken_inside_a_run_are_not_linked(sessions):
    run_token = run_id_var.set("some-other-run")
    trace_token = trace_id_var.set("inherited-trace")
    try:
        await request_links.link_request(RUN, "cancel")
    finally:
        trace_id_var.reset(trace_token)
        run_id_var.reset(run_token)
    assert _links(sessions) == []


def test_log_lines_carry_the_run_id_without_overriding_an_explicit_one():
    token = run_id_var.set(RUN)
    try:
        assert add_user_context(None, "info", {"event": "x"})["run_id"] == RUN
        assert add_user_context(None, "info", {"event": "x", "run_id": "other"})["run_id"] == "other"
    finally:
        run_id_var.reset(token)
