"""Durable, idempotent tool side-effect execution and recovery."""

from __future__ import annotations
from core.harness import log_records

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState, UserMsg
from agentscope.model import ChatResponse, ChatUsage
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.tool import FunctionTool, Toolkit
from agentscope.tool._response import ToolChunk, ToolResponse
from core.db.engine import Base
from core.db.models import ChatRun, ChatSession, ToolCallLog, ToolEffectLease, ToolEffectLedger, ToolEffectReceipt
from features.automation.models import ScheduledTask
from core.llm.runtime.state import AgentRuntimeState
from tests.orchestration.hook_support import make_adapter, durable_adapter
from core.runs.tool_effect_recovery import _plain_replay_args, reconcile_scheduled_task_intent
from core.runs.journal import RunJournal
from core.runs.tool_effects.registry import DEFAULT_TOOL_RECOVERY_REGISTRY, ToolRecoveryRegistry
from core.runs.tool_effects.errors import ToolEffectError, ToolIdempotencyConflict, ToolIntentCommitError, ToolOutcomeUnknown
from core.runs.tool_effects.gateway import ToolEffectGateway
from core.runs.tool_effects.journal import ToolEffectJournal
from core.runs.tool_effects.types import ToolIntent
from core.runs.tool_effects.recovery import recover_incomplete_tool_effects
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def effect_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool-effects.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def make_run(
        run_id: str,
        *,
        owner: str = "worker",
        user_id: str = "user-1",
        chat_id: str = "chat-1",
    ) -> None:
        with sessions() as db:
            if db.get(ChatSession, chat_id) is None:
                db.add(ChatSession(chat_id=chat_id, user_id=user_id, title="test"))
                db.commit()
        journal = RunJournal(sessions)
        journal.accept(
            run_id=run_id,
            message_id=f"msg-{run_id}",
            chat_id=chat_id,
            user_id=user_id,
            request_payload={"kind": "chat"},
            recovery_snapshot={"kind": "chat", "worker_args": {}},
        )
        assert journal.claim(run_id, owner=owner, lease_seconds=300)

    yield sessions, make_run
    engine.dispose()
