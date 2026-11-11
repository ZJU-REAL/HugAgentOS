"""Memory through completed turns, tasks, the agent's tools and the workspace editor.

The platform journals completed conversations and task results into journal/YYYY-MM-DD.md;
the agent writes topic pages under memory/ through the memory store, citing its turn's
source id; the editor saves memory files through the same store.
"""
import personal_agent.workspace.constants as _pa_constants
import personal_agent.workspace.files as _pa_files
import personal_agent.workspace.prompting as _pa_prompting
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from personal_agent.workspace import persona
from personal_agent.models import PersonalAgent
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from tests.test_personal_agent_routing import sessions, personal_agent_on, turn, scripted


@pytest.fixture
def workspace(sessions, monkeypatch, tmp_path):
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    monkeypatch.setattr(_pa_prompting, "prompt_part", lambda part: (_pa_constants.PROMPT_DIR / (part + ".system.md")).read_text().strip())
    with sessions() as db:
        agent = AgentContext(db, "owner").identity.ensure()
    persona.seed(agent["id"])
    return agent, _pa_files.local_workspace(agent["id"])


def page(name, line, description="Q4 发布计划"):
    return ("---\nname: " + name + "\ndescription: " + description + "\naliases: [\"发布\"]\ntype: "
            + name.split("-")[0] + "\nstatus: active\nupdated: 2026-11-09\n---\n## 安排\n- " + line + "\n")


def versions(sessions, agent_id, path):
    from personal_agent.memory import versions as memory_versions
    with sessions() as db:
        return [memory_versions.as_dict(row) for row in memory_versions.listing(db, agent_id, path=path)]


# ---------------------------------------------------------------- the platform journal

def test_completed_conversation_is_journaled_without_model_file_calls(workspace, sessions):
    agent, root = workspace
    with sessions() as db:
        msg = admit(AgentContext(db, "owner"), "Remember the decision: ship on Monday", "journal-turn")
    asyncio.run(turn(msg["message_id"], scripted("The launch is scheduled for Monday.")))
    from personal_agent.workspace.file_memory import drain
    drain(sessions)
    daily = list((root / "journal").glob("*.md"))
    assert len(daily) == 1
    text = daily[0].read_text()
    assert "Remember the decision: ship on Monday" in text
    assert "The launch is scheduled for Monday." in text
    # Journals never land in the wiki.
    assert not [p for p in (root / "memory").glob("*.md") if p.name[0].isdigit()]
    drain(sessions)
    assert daily[0].read_text() == text


@pytest.mark.parametrize("size", [20, 9000])
def test_task_completion_records_result_even_without_report_turn(workspace, sessions, size):
    from personal_agent.workspace.file_memory import drain
    agent, root = workspace
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Prepare the launch", "task-journal")
        svc.tasks.complete(task["id"], "Verified result " + "x" * size, {})
    drain(sessions)
    text = next((root / "journal").glob("*.md")).read_text()
    assert "Prepare the launch" in text and "Verified result" in text
    assert text.count("<!-- pa-journal:") == 1


def test_missing_workspace_retries_without_losing_result(workspace, sessions, monkeypatch):
    from personal_agent.workspace import file_memory
    agent, root = workspace
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "Keep the meeting outcome", "retry")
    asyncio.run(turn(entry["message_id"], scripted("Approved the launch.")))
    moved = root.with_name("offline")
    root.rename(moved)
    file_memory.drain(sessions)
    moved.rename(root)
    monkeypatch.setattr(file_memory.time, "time", lambda: 9999999999)
    file_memory.drain(sessions)
    assert "Approved the launch." in next((root / "journal").glob("*.md")).read_text()


def test_crash_after_file_commit_does_not_duplicate(workspace, sessions, monkeypatch):
    from personal_agent.workspace import file_memory
    agent, root = workspace
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "Review completed", "crash")
    asyncio.run(turn(entry["message_id"], scripted("All checks passed.")))
    original = file_memory.append
    def crash(*args):
        original(*args)
        raise RuntimeError("simulated crash before receipt commit")
    monkeypatch.setattr(file_memory, "append", crash)
    file_memory.drain(sessions)
    monkeypatch.setattr(file_memory, "append", original)
    monkeypatch.setattr(file_memory.time, "time", lambda: 9999999999)
    file_memory.drain(sessions)
    assert next((root / "journal").glob("*.md")).read_text().count("All checks passed.") == 1


def test_journal_uses_completion_timezone_and_redacts_secrets(workspace, sessions):
    from personal_agent.workspace import file_memory
    from core.db.models import ChatMessage
    from personal_agent.models import PersonalAgent
    agent, root = workspace
    with sessions() as db:
        row = ChatMessage(message_id="tz-journal", chat_id=agent["chat_id"], role="assistant", content="password=very-secret-value", chat_seq=1)
        db.add(row)
        owner = db.get(PersonalAgent, agent["id"])
        owner.config = {**owner.config, "timezone": "Asia/Shanghai"}
        file_memory.admit(row, owner, "conversation", input_text="api_key=hidden-credential", at=datetime(2026, 10, 15, 23, tzinfo=ZoneInfo("UTC")))
        db.commit()
        receipt = db.get(ChatMessage, "tz-journal").extra_data[file_memory.KEY]
    assert receipt["day"] == "2026-10-16"
    file_memory.drain(sessions)
    text = (root / "journal" / "2026-10-16.md").read_text()
    assert "very-secret-value" not in text and "hidden-credential" not in text
    assert "[REDACTED]" in text and "<!-- pa-journal:" in text


def test_cancelled_task_is_journaled(workspace, sessions):
    from personal_agent.tasks import task_ops
    from personal_agent.workspace import file_memory
    from core.db.models import ChatMessage
    agent, root = workspace
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Prepare a cancelled report", "cancel-task")
        entry = admit(svc, "Stop that task", "stop-task")
        task_ops.control(svc, db.get(ChatMessage, entry["message_id"]), task["id"], "cancel")
        db.commit()
    file_memory.drain(sessions)
    files = list((root / "journal").glob("*.md"))
    assert files and "task_cancelled" in files[0].read_text()


def test_daily_journal_does_not_finish_first_run(workspace, sessions):
    from personal_agent.workspace.file_memory import drain
    agent, root = workspace
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "Hello", "first-hello")
    asyncio.run(turn(entry["message_id"], scripted("Hello, let us get to know each other.")))
    drain(sessions)
    assert persona.settle_bootstrap(agent["id"], {})
    assert (root / "BOOTSTRAP.md").exists()


def test_journal_rejects_special_files_without_blocking(workspace, monkeypatch):
    import os
    from personal_agent.workspace import file_memory
    agent, root = workspace
    (root / "journal").mkdir()
    target = root / "journal" / "2026-10-16.md"
    os.mkfifo(target)
    original = os.open
    def checked(path, flags, *args, **kwargs):
        if str(path) == target.name:
            assert flags & os.O_NONBLOCK, "Special file opens must not block the delivery queue"
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", checked)
    with pytest.raises(ValueError, match="not a file"):
        file_memory.append(agent["id"], "fifo", {"day": "2026-10-16", "at": "now", "source": "test"}, "result")


@pytest.mark.parametrize("value", ['{"password": "secret with spaces"}', "'api_key': 'secret with spaces'"])
def test_quoted_credentials_are_redacted(value):
    from personal_agent.memory.redact import redact
    assert "secret with spaces" not in redact(value)
    assert "[REDACTED]" in redact(value)


def test_large_journal_keeps_latest_records_readable_and_is_read_only(workspace, sessions):
    agent, root = workspace
    (root / "journal").mkdir()
    path = "journal/2026-10-16.md"
    (root / path).write_text("x" * (_pa_constants.READ_MAX_BYTES + 100) + "\nLatest decision")
    assert "Latest decision" in _pa_files.read_text(agent["id"], path)
    result = _pa_files.read_file(agent["id"], path)
    assert result["truncated"] and not result["editable"]
    assert "Latest decision" in result["content"]
    with pytest.raises(PermissionError):
        _pa_files.write_file(agent["id"], path, "Truncated replacement", result["etag"])

    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "Read recent daily records", "large-daily")
    observed = []
    async def capture(plan):
        observed.extend(plan["results"])
    asyncio.run(turn(entry["message_id"], scripted(("pa_memory_read", {"path": path, "offset": 2}), capture, "Read.")))
    assert observed[0]["lines"] == 3 and observed[0]["content"] == "Latest decision" and not observed[0]["more"]


def test_process_writers_do_not_lose_or_duplicate_entries(workspace):
    import multiprocessing
    import time
    from personal_agent.workspace import file_memory
    agent, root = workspace
    context = multiprocessing.get_context("fork")
    ready = context.Barrier(2)
    def write_entries(index):
        ready.wait(timeout=5)
        for key in ("shared", "unique-" + str(index)):
            for attempt in range(100):
                try:
                    file_memory.append(agent["id"], key, {"day": "2026-10-16", "at": "now", "source": "test"}, key)
                    break
                except BlockingIOError:
                    time.sleep(.01)
            else:
                raise RuntimeError("Lock never released")
    workers = [context.Process(target=write_entries, args=(index,)) for index in range(2)]
    try:
        for worker in workers: worker.start()
        for worker in workers:
            worker.join(timeout=10)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive(): worker.terminate(); worker.join(timeout=3)
    text = (root / "journal/2026-10-16.md").read_text()
    assert text.count("<!-- pa-journal:") == 3
    assert "> shared" in text and "> unique-0" in text and "> unique-1" in text


def test_deleted_conversation_receipt_does_not_block_other_users(workspace, sessions):
    from core.db.models import ChatMessage, ChatSession
    from personal_agent.models import PersonalAgent
    from personal_agent.workspace import file_memory
    agent, root = workspace
    with sessions() as db:
        owner = db.get(PersonalAgent, agent["id"])
        db.add(ChatSession(chat_id="deleted-memory-chat", user_id="owner", title="Removed", deleted_at=datetime.now(ZoneInfo("UTC"))))
        db.flush()
        for index in range(50):
            row = ChatMessage(message_id="deleted-" + str(index), chat_id="deleted-memory-chat", role="assistant", content="Removed")
            file_memory.admit(row, owner, "conversation")
            db.add(row)
        db.commit()
    with sessions() as db:
        entry = admit(AgentContext(db, "owner"), "Surviving journal entry", "not-blocked")
    asyncio.run(turn(entry["message_id"], scripted("Still written.")))
    for _ in range(3): file_memory.drain(sessions)
    assert "Still written." in next((root / "journal").glob("*.md")).read_text()


def test_sparse_oversized_journal_is_rejected_before_allocation(workspace):
    from personal_agent.workspace import file_memory
    agent, root = workspace
    (root / "journal").mkdir()
    path = root / "journal/2026-10-16.md"
    with path.open("wb") as stream: stream.truncate(256 * 1024 * 1024)
    with pytest.raises(ValueError, match="budget"):
        file_memory.append(agent["id"], "oversized", {"day": "2026-10-16", "at": "now", "source": "test"}, "Keep pending")
    assert path.stat().st_size == 256 * 1024 * 1024
