from datetime import datetime, timezone

from core.db.models import KBDocument, KBSpace, UserShadow
from core.db.repository import KBRepository
from core.kb.document_status import classify_document_status, summarize_document_statuses


def test_classify_document_status_normalizes_local_and_external_values():
    for status in ("processing", "indexing", "waiting", "pending", "finalizing"):
        assert classify_document_status(status) == "processing"
    for status in ("failed", "error"):
        assert classify_document_status(status) == "failed"
    for status in ("completed", "completed_indexing", ""):
        assert classify_document_status(status) == "indexed"


def test_summarize_document_statuses_counts_the_full_input():
    items = [
        {"indexing_status": "completed"},
        {"indexing_status": "completed"},
        {"indexing_status": "indexing"},
        {"indexing_status": "error"},
    ]

    assert summarize_document_statuses(items) == {
        "indexed": 2,
        "processing": 1,
        "failed": 1,
    }


def test_repository_counts_all_documents_not_only_the_current_page(db_session):
    db_session.add(UserShadow(user_id="user-kb-stats", username="KB Stats"))
    db_session.add(
        KBSpace(
            kb_id="kb-stats",
            user_id="user-kb-stats",
            name="统计测试库",
            visibility="private",
        )
    )
    statuses = ["completed"] * 23 + ["processing"] * 3 + ["failed"] * 2
    for index, status in enumerate(statuses):
        db_session.add(
            KBDocument(
                document_id=f"doc-stats-{index}",
                kb_id="kb-stats",
                title=f"统计文档 {index}",
                filename=f"stats-{index}.txt",
                size_bytes=1,
                mime_type="text/plain",
                storage_key=f"kb/stats-{index}.txt",
                indexing_status=status,
                uploaded_at=datetime.now(timezone.utc),
            )
        )
    db_session.commit()

    repository = KBRepository(db_session)
    page_items, total = repository.list_documents("kb-stats", page=1, page_size=20)

    assert len(page_items) == 20
    assert total == 28
    assert repository.count_document_statuses("kb-stats") == {
        "indexed": 23,
        "processing": 3,
        "failed": 2,
    }
