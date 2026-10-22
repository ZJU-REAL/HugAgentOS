"""User deletion removes only owned knowledge in the same transaction."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import core.db.models
from core.db.engine import Base
from core.db.models import UserShadow
from features.knowledge.models import KBSpace, KBDocument


@pytest.mark.parametrize("commit", [True, False])
def test_delete_user_cleans_knowledge_and_preserves_rollback(commit):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([UserShadow(user_id="one", username="one"), UserShadow(user_id="two", username="two")])
        session.flush()
        session.add_all([KBSpace(kb_id="a", user_id="one", name="A"), KBSpace(kb_id="b", user_id="two", name="B")])
        session.flush()
        session.add(KBDocument(document_id="doc", kb_id="a", title="doc", filename="doc.txt", size_bytes=1, mime_type="text/plain", storage_key="doc"))
        session.commit()
        session.delete(session.get(UserShadow, "one"))
        session.flush()
        assert session.get(KBSpace, "a") is None
        assert session.get(KBDocument, "doc") is None
        assert session.get(KBSpace, "b") is not None
        if commit:
            session.commit()
        else:
            session.rollback()
        assert (session.get(UserShadow, "one") is None) is commit
        assert (session.get(KBSpace, "a") is None) is commit
        assert (session.get(KBDocument, "doc") is None) is commit
    engine.dispose()
