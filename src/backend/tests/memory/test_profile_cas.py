from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

from core.db.models import ProfileMemory
from core.memory.context import MemoryContext
from core.memory import profile as P
from core.memory import profile_store as S


def _configure(db_session, monkeypatch, *, max_chars=80):
    factory = sessionmaker(bind=db_session.get_bind())
    monkeypatch.setattr(S, "SessionLocal", factory)
    monkeypatch.setattr(
        P,
        "settings",
        SimpleNamespace(memory=SimpleNamespace(enabled=True, profile_max_chars=max_chars)),
    )
    return factory


def test_profile_store_rejects_stale_revision(db_session, monkeypatch):
    factory = _configure(db_session, monkeypatch)
    with factory() as db:
        db.add(
            ProfileMemory(
                user_id="u1",
                workspace_id="default",
                content_md="old",
                revision=0,
            )
        )
        db.commit()

    snapshot = S.read_snapshot("u1", "default")
    assert S.commit_snapshot(snapshot, "first") is True
    assert S.commit_snapshot(snapshot, "stale overwrite") is False

    current = S.read_snapshot("u1", "default")
    assert current.content_md == "first"
    assert current.revision == 1


@pytest.mark.asyncio
async def test_compaction_reloads_and_recomputes_after_concurrent_update(db_session, monkeypatch):
    factory = _configure(db_session, monkeypatch, max_chars=80)
    initial = (
        "- **identity.name**: 小明\n"
        "- **preference.format**: very long preference that makes this profile exceed the limit"
    )
    with factory() as db:
        db.add(
            ProfileMemory(
                user_id="u1",
                workspace_id="default",
                content_md=initial,
                revision=0,
            )
        )
        db.commit()

    ctx = MemoryContext(user_id="u1", message_id="m-cas", write_enabled=True)
    monkeypatch.setattr(P, "_schedule_compact", lambda *_args, **_kwargs: None)
    llm_inputs = []

    async def fake_compact_llm(content, _max_chars):
        llm_inputs.append(content)
        if len(llm_inputs) == 1:
            P._upsert_fields_sync(
                ctx,
                [
                    {
                        "key": "identity.role",
                        "value": "负责人",
                        "reason": "concurrent update",
                        "hits": [],
                    }
                ],
            )
            return "- **identity.name**: 小明"
        assert "identity.role" in content
        return "- **identity.name**: 小明\n- **identity.role**: 负责人"

    monkeypatch.setattr(P, "_run_compact_llm", fake_compact_llm)
    monkeypatch.setattr(P, "audit_record", lambda *_args, **_kwargs: _async_none())

    assert await P.compact(ctx) is True

    current = S.read_snapshot("u1", "default")
    assert "identity.role" in current.content_md
    assert current.revision == 2
    assert len(llm_inputs) == 2


async def _async_none():
    return None
