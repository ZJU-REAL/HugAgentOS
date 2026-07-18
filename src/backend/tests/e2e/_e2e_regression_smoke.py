"""Regression smoke — ensure last_message_at instrumentation does not break the existing add_message flow."""

import uuid

from core.db.engine import SessionLocal
from core.db.models import ChatMessage, ChatSession, UserShadow
from core.services.chat_service import ChatService


def main() -> None:
    with SessionLocal() as db:
        uid = "u_reg"
        cid = f"cr_{uuid.uuid4().hex[:8]}"
        if not db.query(UserShadow).filter(UserShadow.user_id == uid).first():
            db.add(UserShadow(user_id=uid, username="reg"))
        db.add(ChatSession(chat_id=cid, user_id=uid, title="regression"))
        db.commit()

        svc = ChatService(db)
        svc.add_message(chat_id=cid, role="user", content="hello")
        svc.add_message(chat_id=cid, role="assistant", content="hi there")
        svc.add_message(
            chat_id=cid,
            role="tool",
            content='{"result": "ok"}',
            tool_calls=None,
        )

        s = db.query(ChatSession).filter(ChatSession.chat_id == cid).first()
        msgs = (
            db.query(ChatMessage)
            .filter(ChatMessage.chat_id == cid)
            .order_by(ChatMessage.created_at)
            .all()
        )
        print(
            f"message_count={s.message_count} "
            f"last_message_at_set={s.last_message_at is not None} "
            f"got_msgs={len(msgs)} "
            f"roles={[m.role for m in msgs]}"
        )

        # Second call to add_message must also bump last_message_at monotonically
        lma1 = s.last_message_at
        import time
        time.sleep(1.1)
        svc.add_message(chat_id=cid, role="user", content="again")
        db.refresh(s)
        lma2 = s.last_message_at
        print(f"monotonic_increase={lma2 > lma1}")

        # Cleanup
        db.query(ChatMessage).filter(ChatMessage.chat_id == cid).delete()
        db.query(ChatSession).filter(ChatSession.chat_id == cid).delete()
        db.query(UserShadow).filter(UserShadow.user_id == uid).delete()
        db.commit()


if __name__ == "__main__":
    main()
