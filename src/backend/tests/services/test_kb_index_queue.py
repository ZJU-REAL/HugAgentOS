"""知识库文档索引队列的语义测试。

这条队列是为修一类线上事故而生的：索引原本挂在 FastAPI ``BackgroundTasks`` 上，
既抢请求线程池（批量上传把后端整体拖停、网关报 502），又活不过一次进程重启
（文档永远停在「索引中」）。所以这里验的正是那两点的反面——**认领必须互斥**、
**持有者死了必须能回收重排**。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from core.db.engine import Base
from core.db.models import KBDocument, KBSpace, UserShadow
from core.kb import index_queue
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

KB_ID = "kb_idxtest"
USER_ID = "u_idxtest"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/index.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(UserShadow(user_id=USER_ID, username="idx-tester"))
    session.add(KBSpace(kb_id=KB_ID, user_id=USER_ID, name="测试库", visibility="private"))
    session.commit()
    yield session
    session.close()


def _add_doc(db, document_id: str, *, status: str = "processing", extra=None) -> KBDocument:
    doc = KBDocument(
        document_id=document_id,
        kb_id=KB_ID,
        title=f"文档 {document_id}",
        filename=f"{document_id}.md",
        size_bytes=10,
        mime_type="text/markdown",
        storage_key=f"key/{document_id}",
        indexing_status=status,
        extra_data=extra or {},
    )
    db.add(doc)
    db.commit()
    return doc


def test_enqueue_persists_params_for_a_later_process(db):
    """索引参数必须落库——认领它的可能是重启后的下一个进程，拿不到请求上下文。"""
    doc = _add_doc(db, "doc_a", status="completed")
    index_queue.enqueue(
        db,
        doc,
        user_id=USER_ID,
        chunk_method="structured",
        indexing_config={"parent_chunk_size": 512},
        index_modes=["rag", "wiki"],
    )

    snapshot = index_queue.claim_next(db, "worker-1")
    assert snapshot is not None
    assert snapshot["user_id"] == USER_ID
    assert snapshot["chunk_method"] == "structured"
    assert snapshot["indexing_config"] == {"parent_chunk_size": 512}
    assert snapshot["index_modes"] == ["rag", "wiki"]
    assert snapshot["storage_key"] == "key/doc_a"


def test_claim_is_exclusive(db):
    """同一篇文档只能被认领一次，否则会重复索引、写出重复分块。"""
    _add_doc(db, "doc_a")

    first = index_queue.claim_next(db, "worker-1")
    second = index_queue.claim_next(db, "worker-2")

    assert first is not None and first["document_id"] == "doc_a"
    assert second is None  # 队列里没有别的可认领的了

    doc = db.query(KBDocument).filter(KBDocument.document_id == "doc_a").one()
    assert doc.indexing_status == index_queue.STATUS_RUNNING
    assert doc.extra_data["indexing_claimed_by"] == "worker-1"


def test_claim_order_is_fifo(db):
    """先传的先索引：批量上传时用户看到的完成顺序应与上传顺序一致。"""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for idx, name in enumerate(["doc_a", "doc_b", "doc_c"]):
        doc = _add_doc(db, name)
        doc.uploaded_at = base + timedelta(minutes=idx)
    db.commit()

    assert index_queue.claim_next(db, "w")["document_id"] == "doc_a"
    assert index_queue.claim_next(db, "w")["document_id"] == "doc_b"
    assert index_queue.claim_next(db, "w")["document_id"] == "doc_c"


def test_running_doc_is_not_reclaimed_while_heartbeat_is_fresh(db):
    """心跳还在跳的任务不能被抢走——否则一篇文档会被两个 worker 同时索引。"""
    _add_doc(db, "doc_a")
    index_queue.claim_next(db, "worker-1")

    assert index_queue.release_stale(db) == 0
    assert index_queue.claim_next(db, "worker-2") is None


def test_dead_holder_is_recovered_and_requeued(db):
    """持有者进程没了（容器重建/被 kill），文档必须回到队列，而不是永远停在索引中。

    这正是老实现缺的一环：BackgroundTasks 是进程内的，重启即丢，线上因此留下过
    一批刷新也不自愈的僵死文档。
    """
    _add_doc(db, "doc_a")
    index_queue.claim_next(db, "worker-dead")

    # 把心跳拨回到超过僵尸窗口之前
    doc = db.query(KBDocument).filter(KBDocument.document_id == "doc_a").one()
    stale = datetime.now(timezone.utc) - index_queue.STALE_AFTER - timedelta(minutes=1)
    doc.extra_data = {**doc.extra_data, "indexing_heartbeat": stale.isoformat()}
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(doc, "extra_data")
    db.commit()

    assert index_queue.release_stale(db) == 1
    assert (
        db.query(KBDocument).filter(KBDocument.document_id == "doc_a").one().indexing_status
        == index_queue.STATUS_QUEUED
    )

    revived = index_queue.claim_next(db, "worker-new")
    assert revived is not None
    assert revived["attempt"] == 2  # 第二次尝试


def test_heartbeat_refreshes_inflight_docs(db):
    _add_doc(db, "doc_a")
    index_queue.claim_next(db, "worker-1")
    doc = db.query(KBDocument).filter(KBDocument.document_id == "doc_a").one()
    first_beat = doc.extra_data["indexing_heartbeat"]

    index_queue.heartbeat(db, ["doc_a"])
    db.expire_all()
    doc = db.query(KBDocument).filter(KBDocument.document_id == "doc_a").one()
    assert doc.extra_data["indexing_heartbeat"] >= first_beat


def test_retries_then_gives_up_with_a_visible_reason(db):
    """反复失败的文档最终要落 failed 并带上原因，不能无限占着并发位。"""
    _add_doc(db, "doc_a")

    for _ in range(index_queue.MAX_ATTEMPTS):
        assert index_queue.claim_next(db, "w") is not None
        index_queue.give_up_or_requeue(db, "doc_a", "boom")

    doc = db.query(KBDocument).filter(KBDocument.document_id == "doc_a").one()
    assert doc.indexing_status == index_queue.STATUS_FAILED
    assert "boom" in doc.extra_data["indexing_error"]
    # 认领痕迹要清干净，免得下次重排时被僵尸回收逻辑误读
    assert "indexing_claimed_by" not in doc.extra_data


def test_deleted_documents_are_never_claimed(db):
    doc = _add_doc(db, "doc_a")
    doc.deleted_at = datetime.now(timezone.utc)
    db.commit()

    assert index_queue.claim_next(db, "w") is None
    assert index_queue.queue_depth(db) == 0


def test_pending_document_ids_covers_both_queued_and_running(db):
    _add_doc(db, "doc_a")
    _add_doc(db, "doc_b")
    _add_doc(db, "doc_done", status="completed")
    index_queue.claim_next(db, "w")  # doc_a → indexing

    assert sorted(index_queue.pending_document_ids(db, KB_ID)) == ["doc_a", "doc_b"]


def test_claim_stops_after_the_attempt_ceiling(db):
    """反复把 worker 带走的文档（解析时 OOM 之类）不能被无限重排。

    这类任务永远走不到失败分支——它是被僵尸回收捡回来的，所以上限必须在认领这一
    侧兜住，否则它会一直循环、长期占着一个并发位。
    """
    _add_doc(db, "doc_a", extra={"indexing_attempt": index_queue.MAX_ATTEMPTS})

    assert index_queue.claim_next(db, "w") is None
    doc = db.query(KBDocument).filter(KBDocument.document_id == "doc_a").one()
    assert doc.indexing_status == index_queue.STATUS_FAILED
    assert "停止重试" in doc.extra_data["indexing_error"]


def test_legacy_documents_fall_back_to_the_space_config(db):
    """升级前就卡在 processing 的老文档没有随身参数，要从知识库现配置回推。

    这些文档在新 worker 起来时会被直接接手——参数缺省不能让它们用错分块方式，
    更不能把 user_id 留空（向量行按它打标，空了检索侧就对不上）。
    """
    space = db.query(KBSpace).filter(KBSpace.kb_id == KB_ID).one()
    space.chunk_method = "laws"
    space.extra_data = {"indexing_config": {"parent_chunk_size": 777}}
    db.commit()

    _add_doc(db, "doc_legacy")  # 没有 indexing_params
    snapshot = index_queue.claim_next(db, "w")

    assert snapshot["chunk_method"] == "laws"
    assert snapshot["user_id"] == USER_ID
    assert snapshot["indexing_config"] == {"parent_chunk_size": 777}
