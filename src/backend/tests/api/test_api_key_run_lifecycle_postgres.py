"""PostgreSQL transaction ordering for credential revocation and admission.

Only run against an isolated test schema, never production business data.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

from core.db.models import ChatRun, ChatSession, UserApiKey, UserShadow
from core.auth.api_key_runs import cancel_key_runs, require_admission_key
from core.auth.api_keys import ApiKeyService
from core.chat.sequencer import ChatSequencer
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker


def check_ordering(engine, revoke_first):
    sessions = sessionmaker(bind=engine)
    key_id = "key-revoke-first" if revoke_first else "key-admit-first"
    chat_id = "chat-" + key_id
    payload = {"api_key_id": key_id}
    with sessions() as db:
        if db.get(UserShadow, "owner") is None:
            db.add(UserShadow(user_id="owner", username="test-owner"))
            db.commit()
        db.add(ChatSession(chat_id=chat_id, user_id="owner"))
        db.add(
            UserApiKey(
                id=key_id,
                user_id="owner",
                name="test",
                key_prefix="test",
                key_hash=key_id,
                enabled=True,
            )
        )
        db.commit()
    held, release, second_started = Event(), Event(), Event()

    def first():
        with sessions() as db:
            if revoke_first:
                key = db.query(UserApiKey).filter_by(id=key_id).with_for_update().one()
                key.enabled = False
                cancel_key_runs(db, key_id, "owner", datetime.now(timezone.utc))
                held.set()
                assert release.wait(5)
                db.commit()
            else:
                require_admission_key(db, payload, "owner", datetime.now(timezone.utc))
                held.set()
                assert release.wait(5)
                return (
                    ChatSequencer(db)
                    .accept_main_run(
                        chat_id=chat_id,
                        user_id="owner",
                        user_content="hello",
                        request_payload=payload,
                    )
                    .run.run_id
                )

    def second():
        assert held.wait(5)
        second_started.set()
        with sessions() as db:
            if revoke_first:
                try:
                    ChatSequencer(db).accept_main_run(
                        chat_id=chat_id,
                        user_id="owner",
                        user_content="hello",
                        request_payload=payload,
                    )
                except HTTPException as error:
                    assert error.status_code == 401
                else:
                    raise AssertionError("revoked credential admitted a run")
            else:
                assert ApiKeyService(db).set_enabled("owner", key_id, False) is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(first)
        follower = pool.submit(second)
        try:
            assert held.wait(5) and second_started.wait(5)
        finally:
            release.set()
        run_id = winner.result(timeout=10)
        follower.result(timeout=10)
    with sessions() as db:
        if revoke_first:
            assert db.query(ChatRun).filter_by(chat_id=chat_id).count() == 0
        else:
            assert db.get(ChatRun, run_id).status == "cancelled"
    print(
        "PASS PostgreSQL", "revoke-before-admission" if revoke_first else "admission-before-revoke"
    )
