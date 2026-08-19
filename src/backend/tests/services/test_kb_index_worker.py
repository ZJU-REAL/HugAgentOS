"""索引 worker 执行面的接线测试。

队列语义在 ``test_kb_index_queue.py`` 里验；这里验的是 worker 把队列快照兑现成一次
真正索引调用的那一段：原文从存储按 ``storage_key`` 取回（而不是像老实现那样让文件
字节跟着任务在内存里排队），入队时存下的参数原样传给索引函数，取不回原文时按一次
失败结算而不是把任务默默丢掉。
"""

from __future__ import annotations

import pytest
from core.kb.index_worker import KBIndexWorker, _concurrency


class _FakeStorage:
    def __init__(self, payload: bytes | None = None, boom: Exception | None = None):
        self.payload = payload
        self.boom = boom
        self.asked_for: list[str] = []

    def download_bytes(self, key: str) -> bytes:
        self.asked_for.append(key)
        if self.boom is not None:
            raise self.boom
        assert self.payload is not None
        return self.payload


@pytest.fixture
def snapshot():
    return {
        "document_id": "doc_a",
        "kb_id": "kb_a",
        "title": "标题",
        "mime_type": "text/markdown",
        "storage_key": "kb_documents/doc_a.md",
        "attempt": 1,
        "user_id": "u_a",
        "chunk_method": "laws",
        "indexing_config": {"parent_chunk_size": 512},
        "index_modes": ["rag"],
    }


def test_run_one_fetches_source_from_storage_and_passes_params_through(
    monkeypatch, snapshot
):
    storage = _FakeStorage(payload=b"hello world")
    monkeypatch.setattr("core.storage.get_storage", lambda: storage)

    seen = {}
    monkeypatch.setattr(
        "core.content.kb_processing.vectorise_document_background",
        lambda **kwargs: seen.update(kwargs),
    )

    KBIndexWorker()._run_one(snapshot)

    # 原文是 worker 现取的，不是跟着任务在内存里排队等来的
    assert storage.asked_for == ["kb_documents/doc_a.md"]
    assert seen["file_bytes"] == b"hello world"
    # 入队时存下的参数必须原样兑现，否则重启续跑会用错分块方式
    assert seen["chunk_method"] == "laws"
    assert seen["indexing_config"] == {"parent_chunk_size": 512}
    assert seen["index_modes"] == ["rag"]
    assert seen["user_id"] == "u_a"
    assert seen["document_id"] == "doc_a"


def test_run_one_settles_the_document_when_the_source_cannot_be_read(
    monkeypatch, snapshot
):
    """取不回原文不能把任务默默丢掉——那正是文档永远停在「索引中」的老毛病。"""
    monkeypatch.setattr(
        "core.storage.get_storage", lambda: _FakeStorage(boom=OSError("gone"))
    )
    monkeypatch.setattr(
        "core.content.kb_processing.vectorise_document_background",
        lambda **kwargs: pytest.fail("原文都没取到，不该进索引"),
    )

    settled = {}
    monkeypatch.setattr(
        "core.kb.index_queue.give_up_or_requeue",
        lambda db, document_id, error: settled.update(id=document_id, error=error),
    )

    KBIndexWorker()._run_one(snapshot)

    assert settled["id"] == "doc_a"
    assert "读取原文失败" in settled["error"]


def test_concurrency_is_bounded(monkeypatch):
    """并发要有上限：索引里最重的是 embedding 与逐块 LLM 调用，开太大会把向量服务
    和 DB 连接池一起顶穿（历史上 39 篇并发直接打出 QueuePool 超时）。"""
    monkeypatch.setenv("KB_INDEX_CONCURRENCY", "999")
    assert _concurrency() == 16
    monkeypatch.setenv("KB_INDEX_CONCURRENCY", "0")
    assert _concurrency() == 1
    monkeypatch.setenv("KB_INDEX_CONCURRENCY", "不是数字")
    assert _concurrency() == 3
