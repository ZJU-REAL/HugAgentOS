"""Shared setup for the Dreaming tests: an agent with a seeded wiki, receipts, a scripted model."""
import asyncio
import itertools
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import personal_agent.workspace.files as _pa_files
from core.db.models import ChatMessage
from core.db.repository.chat import ChatMessageRepository
from personal_agent.context import AgentContext
from personal_agent.conversation.message_routing import admit
from personal_agent.memory import layout, seed, sources, store
from personal_agent.memory.dreaming import pipeline, runs
from personal_agent.models import PersonalAgent
from tests.test_personal_agent import personal_agent_on  # noqa: F401  (module on, user permitted)
from tests.test_personal_agent_routing import sessions  # noqa: F401

NOW = datetime(2026, 11, 9, 4, tzinfo=timezone.utc).timestamp()   # 12:00 in Asia/Shanghai, after the 03:00 slot
DAY = "2026-11-09"
_ORDER = itertools.count()   # receipts get strictly increasing times: the queue order is deterministic


def page_text(name, lines, description="测试页面；用到时读", aliases=None):
    body = "\n".join(lines)
    return ("---\nname: " + name + "\ndescription: " + description + "\naliases: " + json.dumps(aliases or [name], ensure_ascii=False)
            + "\ntype: " + name.split("-")[0] + "\nstatus: active\nupdated: " + DAY + "\n---\n" + body + "\n")


def fact(text, sid, day=DAY):
    return "- " + text + " [src: " + sid + " | as-of: " + day + "]"


def index_text(pages):
    lines = ["# 记忆目录", "", "## Where to start", "- 先看这里，再读页面前 60 行", ""]
    groups = {}
    for name, summary in pages:
        groups.setdefault(layout.TYPE_LABELS[name.split("-")[0]], []).append("- [[" + name + "]]：" + summary)
    for label, rows in groups.items(): lines += ["## " + label, *rows, ""]
    return "\n".join(lines)


@pytest.fixture
def agent(sessions, monkeypatch, tmp_path):
    """(agent id, factory); the wiki is seeded and the factory does not autoflush, like production."""
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    sessions.configure(autoflush=False)
    with sessions() as db:
        view = AgentContext(db, "owner").identity.ensure()
    _pa_files.local_workspace(view["id"]).mkdir(parents=True, exist_ok=True)
    seed.seed(view["id"])
    return view["id"]


def conversation(sessions, text, reply, steps=None):
    """A finished turn whose reply carries a written, undreamed journal receipt; returns the reply's source id."""
    with sessions() as db:
        user = admit(AgentContext(db, "owner"), text, "dream-" + uuid.uuid4().hex)
    with sessions() as db:
        row = db.get(PersonalAgent, _agent_id(db))
        message = ChatMessageRepository(db).create({
            "message_id": "pa_msg_" + uuid.uuid4().hex, "chat_id": row.chat_id, "role": "assistant", "content": reply,
            "model_steps": steps, "created_at": datetime(2026, 11, 9, 3, tzinfo=timezone.utc) + timedelta(seconds=next(_ORDER)),
            "extra_data": {"personal_file_memory": {
                "agent_id": row.id, "status": "written", "day": DAY, "at": DAY + "T11:00:00+08:00",
                "source": "conversation", "input": text, "attempts": 0, "next_attempt": 0}}}, commit=False)
        db.commit()
        return sources.for_message(row, message)


def _agent_id(db):
    return db.query(PersonalAgent).filter(PersonalAgent.user_id == "owner").one().id


def receipt(sessions, sid):
    with sessions() as db:
        return dict(db.get(ChatMessage, sid.split("-", 1)[1]).extra_data["personal_file_memory"])


def write(sessions, ident, rel, text, *, writer=layout.SYSTEM, source_id=None):
    with sessions() as db:
        row = db.get(PersonalAgent, ident)
        store.write(db, row, rel, text, None, writer=writer, source_id=source_id, check=writer != layout.SYSTEM)
        db.commit()


def run_view(sessions):
    with sessions() as db:
        return runs.view(db, AgentContext(db, "owner").identity.agent(), NOW)


# ---------------------------------------------------------------- the scripted model

def call(name, **args):
    from agentscope.message import ToolCallBlock
    return ToolCallBlock(id="call_" + uuid.uuid4().hex[:8], name=name, input=json.dumps(args, ensure_ascii=False))


def say(text):
    from agentscope.message import TextBlock
    return TextBlock(text=text)


class Script:
    """A stand-in chat model. analyzer: one list of replies per analyzer run, organizer: one list.

    A reply is a list of blocks, a callable (messages) -> list of blocks, or an exception to raise.
    Every tool result the model saw is kept in .seen for assertions."""

    def __init__(self, analyzer=(), organizer=()):
        self.queues = {"analyzer": [list(item) for item in analyzer], "organizer": [list(organizer)]}
        self.current = {}
        self.seen = []
        self.tasks = []

    async def __call__(self, messages, tools=None, **_):
        from agentscope.model import ChatResponse
        kind = "organizer" if "organizer" in messages[0].content[0].text[:40] else "analyzer"
        if len(messages) == 2:
            self.tasks.append((kind, messages[1].content[0].text))
            self.current[kind] = self.queues[kind].pop(0) if self.queues[kind] else [[say("{}")]]
        else:
            self.seen += [b.output for b in messages[-1].content if getattr(b, "type", "") == "tool_result"]
        replies = self.current[kind]
        reply = replies.pop(0) if replies else [say("{}")]
        if isinstance(reply, BaseException): raise reply
        if callable(reply): reply = reply(messages)
        return ChatResponse(content=list(reply), is_last=True)


def tick(sessions, script, now=NOW):
    asyncio.run(pipeline.tick(sessions=sessions, now=now, make_model=lambda provider: script))
