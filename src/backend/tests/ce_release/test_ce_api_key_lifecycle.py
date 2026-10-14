"""CE credential revocation fences shared durable run execution."""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models import ChatRun, UserApiKey, UserShadow
from core.services.api_key_runs import cancel_key_runs, require_admission_key
from core.services.run_journal import RunJournal


def test_ce_revocation_cancels_only_bound_runs_and_blocks_admission(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'key-lifecycle.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    now = datetime.now(timezone.utc)
    with sessions() as db:
        db.add(UserShadow(user_id="owner", username="Owner"))
        db.add(UserApiKey(id="key", user_id="owner", name="Client",
                          key_prefix="test", key_hash="test", enabled=False))
        for run_id, payload in [("bound", {"api_key_id": "key"}), ("web", {})]:
            db.add(ChatRun(run_id=run_id, user_id="owner", chat_id=run_id,
                           message_id=run_id, status="pending", request_payload=payload))
        db.commit()
        cancel_key_runs(db, "key", "owner", now)
        db.commit()
        assert db.get(ChatRun, "bound").status == "cancelled"
        assert db.get(ChatRun, "web").status == "pending"
        with pytest.raises(HTTPException) as error:
            require_admission_key(db, {"api_key_id": "key"}, "owner", now)
        assert error.value.status_code == 401
    assert not RunJournal(sessions).claim("bound", owner="worker", lease_seconds=30)
    engine.dispose()
