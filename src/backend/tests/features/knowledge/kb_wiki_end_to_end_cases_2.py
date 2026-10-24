from tests.features.knowledge.kb_wiki_end_to_end_support import (
    DOC_ID,
    KBSpace,
    KB_ID,
    LLMBudget,
    USER_ID,
    WikiConfig,
    _wrap_json,
    json,
    pipeline,
    wiki_db,
)

def test_custom_instructions_reach_every_stage(wiki_db, monkeypatch):
    """用户填的要求必须真的送达每个阶段的模型调用。

    配置字段加了却没接到提示词上，是这类功能最典型的"看起来做了"——
    界面能填、能存库、生成结果毫无变化。这里逐阶段核实。
    """
    session, _ = wiki_db
    seen: dict[str, str] = {}

    def _capture(*, system, user, budget=None, purpose="wiki", **kwargs):
        seen[purpose] = f"{system or ''}\n{user}"
        if budget is not None:
            budget.record(len(user), 10)
        if purpose == "候选抽取":
            return json.dumps(
                {
                    "entities": [
                        {"name": "示例科技", "slug": "entity/acme", "aliases": [],
                         "description": "d", "details": "x"}
                    ],
                    "concepts": [],
                }
            )
        if purpose == "引文标注":
            return json.dumps({"citations": {"entity/acme": ["c001"]}, "new_slugs": []})
        if purpose in ("去重判定",):
            return json.dumps({"merges": {}})
        if purpose == "目录规划":
            return json.dumps({"assignments": []})
        if purpose in ("写页", "文档摘要"):
            return "SUMMARY: 摘要\n# 标题\n正文"
        return "{}"

    for module in ("extract", "cite", "dedup", "taxonomy"):
        monkeypatch.setattr(f"features.knowledge.wiki.{module}.call_llm_json", _wrap_json(_capture))
    monkeypatch.setattr('features.knowledge.wiki.reduce.call_llm', _capture)

    pipeline.run_ingest(
        session,
        KB_ID,
        [DOC_ID],
        config=WikiConfig(
            map_parallel=1,
            reduce_parallel=1,
            extraction_instructions="重点关注补助金额与申报条件",
            content_instructions="每页开头先给一句话结论",
        ),
        budget=LLMBudget(max_calls=100),
    )

    # 抽取侧两个阶段拿到抽取要求
    for stage in ("候选抽取", "引文标注"):
        assert "重点关注补助金额与申报条件" in seen[stage], f"{stage} 未收到抽取要求"
        assert "<extraction_business_instructions>" in seen[stage]

    # 撰写侧两个阶段拿到撰写要求
    for stage in ("写页", "文档摘要"):
        assert "每页开头先给一句话结论" in seen[stage], f"{stage} 未收到撰写要求"
        assert "<content_business_instructions>" in seen[stage]

    # 两侧不串味：抽取要求不该出现在写页提示词里
    assert "重点关注补助金额与申报条件" not in seen["写页"]


def test_worker_batches_sibling_ingest_jobs(wiki_db):
    """同库的多个待跑摄入作业要被合并成一批。

    上传是一篇文档一个作业（入队点在索引完成回调里，天然单篇）。若处理时也一篇
    一批，单 worker 串行跑 39 篇要数小时，而且 map_parallel 根本没有多篇文档可并行。
    """
    from features.knowledge.wiki import jobs as job_queue

    session, _ = wiki_db
    first = job_queue.enqueue_ingest(session, KB_ID, ["doc-a"])
    job_queue.enqueue_ingest(session, KB_ID, ["doc-b"])
    job_queue.enqueue_ingest(session, KB_ID, ["doc-c"])

    claimed = job_queue.claim_next_job(session, "worker-1")
    assert claimed is not None and claimed.job_id == first.job_id

    siblings = job_queue.claim_sibling_ingest_jobs(session, "worker-1", KB_ID, limit=4)
    assert [s.payload["document_ids"] for s in siblings] == [["doc-b"], ["doc-c"]]

    # 合批后全部一起结单，不留孤儿
    job_queue.finish_jobs(session, [claimed.job_id, *(s.job_id for s in siblings)])
    assert job_queue.kb_job_summary(session, KB_ID)["generating"] is False


def test_batching_never_crosses_knowledge_bases(wiki_db):
    """跨库合批会让一个大库把其他库饿死。"""
    from features.knowledge.models import KBSpace
    from features.knowledge.wiki import jobs as job_queue

    session, _ = wiki_db
    session.add(
        KBSpace(kb_id="kb_other", user_id=USER_ID, name="别的库", visibility="private",
                extra_data={"index_modes": ["wiki"]})
    )
    session.commit()
    job_queue.enqueue_ingest(session, KB_ID, ["mine"])
    job_queue.enqueue_ingest(session, "kb_other", ["theirs"])

    siblings = job_queue.claim_sibling_ingest_jobs(session, "worker-1", KB_ID, limit=10)
    assert all(s.kb_id == KB_ID for s in siblings)


def test_one_knowledge_base_runs_one_batch_at_a_time(wiki_db):
    """同一个库同时只允许一批在跑。

    同库两批并发会丢更新：写页阶段先把既有页读成内存快照，再逐条基于快照重写、
    最后整体 upsert，而不同文档抽出同名 slug 是常态——后写的会覆盖先写的。
    生产上两个 uvicorn worker 各跑一个 wiki worker 同啃一个库，内存也随之翻倍。
    """
    from features.knowledge.wiki import jobs as job_queue

    session, _ = wiki_db
    job_queue.enqueue_ingest(session, KB_ID, ["doc-a"])
    job_queue.enqueue_ingest(session, KB_ID, ["doc-b"])

    assert job_queue.claim_next_job(session, "worker-1") is not None
    assert job_queue.claim_next_job(session, "worker-2") is None


def test_a_busy_knowledge_base_does_not_block_other_ones(wiki_db):
    """同库串行不能退化成全局串行，否则一个大库会把其他库全堵死。"""
    from features.knowledge.wiki import jobs as job_queue

    session, _ = wiki_db
    session.add(
        KBSpace(
            kb_id="kb_other",
            user_id=USER_ID,
            name="别的库",
            visibility="private",
            extra_data={"index_modes": ["wiki"]},
        )
    )
    session.commit()
    job_queue.enqueue_ingest(session, KB_ID, ["mine"])
    job_queue.enqueue_ingest(session, "kb_other", ["theirs"])

    first = job_queue.claim_next_job(session, "worker-1")
    second = job_queue.claim_next_job(session, "worker-2")
    assert first is not None and second is not None
    assert {first.kb_id, second.kb_id} == {KB_ID, "kb_other"}


def test_a_dead_workers_knowledge_base_is_taken_over(wiki_db):
    """持有者死了（心跳停摆）之后这个库必须能被接管，不能因为互斥而永远卡住。"""
    from features.knowledge.wiki import jobs as job_queue
    from features.knowledge.wiki.jobs import STALE_AFTER, _now

    session, _ = wiki_db
    job_queue.enqueue_ingest(session, KB_ID, ["doc-a"])
    claimed = job_queue.claim_next_job(session, "worker-1")
    assert claimed is not None

    claimed.heartbeat_at = _now() - STALE_AFTER * 2
    session.commit()

    retaken = job_queue.claim_next_job(session, "worker-2")
    assert retaken is not None and retaken.job_id == claimed.job_id
