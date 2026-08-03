"""Seed the evolution console with demonstration data.

**This writes fabricated records.** Nothing here was observed, mined or
measured — it exists so the console can be shown end to end without waiting for
a corpus to accumulate. Every row it writes carries a ``demo-`` id prefix, and
``--clean`` removes exactly those rows and nothing else, so a demo can never be
mistaken for, or tangled up with, real evolution history.

Usage (inside the backend container)::

    python scripts/seed_evolution_demo.py          # insert (re-runnable)
    python scripts/seed_evolution_demo.py --clean  # remove every demo- row

What it populates, and why each piece is shaped the way it is:

* **Episodes + trace events** — the console's evidence tab, and the substrate
  the capability-health tab computes from. Health is *derived*, not stored:
  "offered 14 times, opened 0 times" only appears if the underlying events say
  so, so the events are built to produce the intended verdicts rather than the
  verdicts being written directly.
* **Candidates** across all three engines (memory / skill / orchestration) in
  every review state, because the funnel is a count over states.
* **Releases**, including one guardrail rollback and one manual rollback — the
  overview separates them on purpose, and with only one kind present that
  distinction is invisible.
* **Agent profiles** — the orchestration tab.
* **Memory ops** — applied memory changes with their undo state.

Skill exposure age is read from the release ledger, so skill releases are dated
well into the past; a skill that shipped yesterday is deliberately not judged by
the retirement rules, and demo rows dated today would all be filtered out.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app/src/backend")

from core.db.engine import SessionLocal  # noqa: E402
from core.db.models.evolution import (  # noqa: E402
    EvolutionAgentProfile,
    EvolutionCandidate,
    EvolutionEpisode,
    EvolutionMemoryOp,
    EvolutionRelease,
    EvolutionTraceEvent,
)

PREFIX = "demo-"
NOW = datetime.now(timezone.utc)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def checksum(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


# ── The scenarios on display ────────────────────────────────────────────────
#
# Written as the console would have discovered them: each one names what was
# observed, not what to do about it. A hypothesis that reads "启用 X" tells a
# reviewer nothing they can judge.

MEMORY_CANDIDATES = [
    dict(
        asset="mem:财务分析-主体核验",
        operation="reweight",
        status="active",
        risk="low",
        hypothesis="「做财务分析前先核验公司主体」在 9 个会话中被独立复述，但检索命中率只有 31%——"
        "同一条做法反复被人重新说一遍，说明它没有被稳定召回",
        selected="memory_retrieval",
        confidence=0.86,
        evidence=6,
        ops=[{"operation": "reweight", "memory_ref": "ref-fin-subject",
              "text": "做财务分析前先核验公司主体是否唯一",
              "reason": "被复述 9 次仍召回不稳定，提高检索权重",
              "before": {"weight": 1.0}, "after": {"weight": 1.6}}],
    ),
    dict(
        asset="mem:周报口径-自然周",
        operation="reweight",
        status="canary",
        risk="medium",
        hypothesis="「周报按滚动 7 天」与「周报按自然周（周一至周日）」同时被召回，两条互相矛盾；"
        "后者在最近 30 天被复述 4 次，前者 0 次",
        selected="memory_conflict",
        confidence=0.79,
        evidence=5,
        ops=[{"operation": "reweight", "memory_ref": "ref-weekly-rolling7",
              "text": "周报按滚动 7 天统计",
              "reason": "与「自然周」口径冲突，且 30 天内 0 次复述，降到不再注入",
              "before": {"weight": 1.0}, "after": {"weight": 0.0}}],
    ),
    dict(
        asset="mem:数据出处-标注要求",
        operation="merge",
        status="draft",
        risk="low",
        hypothesis="3 条近重复记忆表达同一条要求（「每个数字要给出处」「数据要写来源」「结论后附引用」）——"
        "重复条目会挤占 Top-K，把不相关的记忆一起带进上下文",
        selected="memory_duplication",
        confidence=0.91,
        evidence=8,
        ops=[{"operation": "merge", "memory_ref": "ref-cite-source",
              "text": "每个数字要给出处（合并「数据要写来源」「结论后附引用」）",
              "reason": "3 条近重复挤占 Top-K，合并为 1 条",
              "before": {"count": 3}, "after": {"count": 1}}],
    ),
    dict(
        asset="mem:某季度会议纪要-临时安排",
        operation="reweight",
        status="draft",
        risk="low",
        hypothesis="该记忆写入 168 天，期间被召回 2 次、两次都未被答案引用——"
        "属于当时的一次性安排，不是可复用做法",
        selected="memory_staleness",
        confidence=0.72,
        evidence=3,
        ops=[{"operation": "reweight", "memory_ref": "ref-meeting-temp",
              "text": "本季度会议纪要统一由行政组汇总",
              "reason": "168 天未被引用，属一次性安排，降到不再注入",
              "before": {"weight": 1.0}, "after": {"weight": 0.0}}],
    ),
    dict(
        asset="mem:PPT配色-先确认再批量",
        operation="reweight",
        status="replay_passed",
        risk="low",
        hypothesis="「配色改动先出一版确认再批量应用」只在 ppt 类任务命中，却在所有任务类型被注入——"
        "限定适用范围可以让其余任务少 1 条无关注入",
        selected="memory_scope",
        confidence=0.83,
        evidence=4,
        ops=[{"operation": "reweight", "memory_ref": "ref-ppt-confirm-first",
              "text": "配色改动先出一版确认再批量应用",
              "reason": "只在 ppt 类任务命中，限定适用范围",
              "before": {"weight": 1.0, "scope": "all"},
              "after": {"weight": 1.4, "scope": "ppt"}}],
    ),
]

SKILL_BODY_CHAIN = """## 不适用范围
- 只做单家企业尽调时不用本技能，直接查企业档案更快
- 客户已给定产业环节口径时，以客户口径为准，不要重新划分

## 何时使用
需要梳理某个产业的上下游结构，并逐环节找出代表企业时。

## 步骤
1. 先用 `industry_chain` 拉取该产业的环节划分，确认口径与客户一致
2. 每个环节用 `internet_search` 补充代表企业，每家必须带来源
3. 用 `write_file` 输出，结论放最前面，再给分环节明细

## 常见失败与处理
- 环节划分与企业归属不一致：以环节口径为准，并在文末注明分歧
- 找不到来源的企业：不写入正文，放到「待核实」小节
"""

SKILL_CANDIDATES = [
    dict(
        asset="industry-chain-mapping",
        operation="new",
        status="draft",
        risk="low",
        tools=["industry_chain", "internet_search", "write_file"],
        doc_desc="梳理产业上下游结构并逐环节找出代表企业，每家带来源",
        doc_body=SKILL_BODY_CHAIN,
        hypothesis="12 个会话走了同一条工具序列（industry_chain → search → write_file），"
        "每次都从零重新规划，平均多花 3.4 轮",
        selected="repeated_tool_subsequence",
        confidence=0.88,
        evidence=12,
    ),
    dict(
        asset="政策文件要点提取",
        operation="new",
        status="shadow",
        risk="medium",
        tools=["read_file", "internet_search", "write_file"],
        hypothesis="7 个会话在「提取政策文件要点」上重复同一套步骤，其中 3 次因为漏掉发文机关被要求返工",
        selected="repeated_tool_subsequence",
        confidence=0.81,
        evidence=7,
    ),
    dict(
        asset="ppt-design",
        operation="patch",
        status="draft",
        risk="medium",
        hypothesis="被打开 11 次，其中 7 次后续动作与文档描述无关——"
        "模型想用它、但按它做不下去，是文档的问题而不是能力的问题",
        selected="skill_ignored_when_opened",
        confidence=0.77,
        evidence=11,
    ),
    dict(
        asset="capability-guide-brief",
        operation="deprecate",
        status="draft",
        risk="high",
        hypothesis="被打开 6 次、也照做了，但只有 33% 的会话最终成功——"
        "低于成功率下限，建议退役而不是继续挂载",
        selected="skill_low_success",
        confidence=0.69,
        evidence=6,
    ),
    dict(
        asset="季度经营分析报告",
        operation="new",
        status="rejected",
        risk="high",
        hypothesis="5 个会话重复同一套报告结构，但样本全部来自同一位用户、同一家主体——"
        "证据不足以支撑一条面向所有人的技能",
        selected="repeated_tool_subsequence",
        confidence=0.44,
        evidence=5,
        reject_reason="证据集中在单一用户，跨用户支持度不足（要求 ≥3 位不同用户）",
    ),
    dict(
        asset="excel-editing",
        operation="patch",
        status="active",
        risk="low",
        hypothesis="与「表格数据清洗」候选声明的工具集重叠 4 个——"
        "两条并存会让技能选择在近重复项之间摇摆",
        selected="skill_overlap",
        confidence=0.74,
        evidence=9,
    ),
]

ORCHESTRATION_CANDIDATES = [
    dict(
        asset="research",
        operation="patch",
        status="active",
        risk="medium",
        hypothesis="research 类任务的 16 次执行中，8 个可见工具一次都没被调用过——"
        "工具定义占了约 4200 tokens 的上下文，却没有参与任何一次决策",
        selected="orchestration_unused_tools",
        confidence=0.9,
        evidence=16,
    ),
    dict(
        asset="report",
        operation="patch",
        status="canary",
        risk="medium",
        hypothesis="report 类任务有 23% 的执行在记忆检索上超时（预算 600ms），"
        "这些执行全部在无记忆注入的情况下作答",
        selected="orchestration_budget",
        confidence=0.85,
        evidence=13,
    ),
    dict(
        asset="chat",
        operation="patch",
        status="shadow",
        risk="low",
        hypothesis="chat 类任务每轮平均挂载 12 个技能候选，模型打开的始终落在前 4 个——"
        "候选过多会稀释选择精度",
        selected="orchestration_selection_precision",
        confidence=0.78,
        evidence=21,
    ),
    dict(
        asset="code_exec",
        operation="patch",
        status="rolled_back",
        risk="high",
        hypothesis="code_exec 类任务尝试把最大迭代轮数从 12 降到 6，以缩短长尾耗时",
        selected="orchestration_budget",
        confidence=0.61,
        evidence=8,
    ),
]

# Skill usage written into the trace events. Each row is built to reach one
# specific verdict on the health tab, so the demo shows the *decremental*
# direction rather than a library that only ever grows.
#
#   offers ≥ 10 且 opens = 0 且 曝光 ≥ 14 天 → 无人问津
#   opens ≥ 3 且 成功率 < 40%                → 建议退役
SKILL_USAGE = [
    dict(skill="pdf-editing", offers=14, opens=0, successes=0),      # 无人问津
    dict(skill="capability-guide-brief", offers=11, opens=6, successes=2),  # 建议退役
    dict(skill="ppt-design", offers=16, opens=11, successes=8),      # 健康
    dict(skill="excel-editing", offers=12, opens=7, successes=6),    # 健康
    dict(skill="word-editing", offers=9, opens=4, successes=3),      # 样本不足，仅入总表
]

EPISODE_SCENARIOS = [
    ("研究 2025 年新能源汽车出口的主要目的地变化", "research", 0.82, "success"),
    ("把这份政策文件的要点提取成一页纸", "report", 0.74, "success"),
    ("做一版季度经营分析的 PPT 提纲", "chat", 0.68, "success"),
    ("帮我核对这张表里的同比口径对不对", "analysis", 0.91, "success"),
    ("整理近三年财政收入结构变化并给出图表", "report", 0.55, "partial"),
    ("这份合同里有哪些需要注意的条款", "chat", 0.79, "success"),
    ("产业链上游有哪些关键环节，各自谁在做", "research", 0.86, "success"),
    ("把上面的结论写成给领导看的汇报稿", "report", 0.71, "success"),
    ("统计一下各地区的完成进度并排序", "analysis", 0.34, "failed"),
    ("生成本周的周报初稿", "chat", 0.77, "success"),
    ("这个数据和上个月对不上，帮我查原因", "analysis", 0.62, "partial"),
    ("给这份方案配一套演示用的配图", "chat", 0.58, "partial"),
]


def _add(db, row):
    """Add a row and hand it back, so the caller can keep mutating it."""
    db.add(row)
    return row


def resolve_user(db, explicit: str = "") -> str:
    """Whose account the demo data belongs to.

    This matters more than it looks. The **user-facing** console
    (设置 → 进化控制台) filters everything by ``episode.user_id`` — contributions,
    the approval queue, the memory counter. Demo rows written under a synthetic
    id populate the admin console at /config and leave the user's own page
    completely empty, which looks exactly like a broken feature.
    """
    if explicit:
        return explicit
    from core.db.models import UserShadow

    row = (
        db.query(UserShadow)
        .filter(~UserShadow.user_id.like(f"{PREFIX}%"))
        .order_by(UserShadow.updated_at.desc())
        .first()
    )
    if row is None:
        raise SystemExit("找不到真实用户，请用 --user <user_id> 指定")
    return row.user_id


def clean(db) -> dict:
    """Remove every demo row. Scoped by the id prefix, so real data is untouched."""
    counts = {}
    for model, column in (
        (EvolutionTraceEvent, EvolutionTraceEvent.event_id),
        (EvolutionEpisode, EvolutionEpisode.episode_id),
        (EvolutionRelease, EvolutionRelease.release_id),
        (EvolutionCandidate, EvolutionCandidate.candidate_id),
        (EvolutionAgentProfile, EvolutionAgentProfile.profile_id),
        (EvolutionMemoryOp, EvolutionMemoryOp.op_id),
    ):
        n = db.query(model).filter(column.like(f"{PREFIX}%")).delete(synchronize_session=False)
        counts[model.__tablename__] = n
    db.commit()
    return counts


def seed_episodes(db, owner: str) -> list:
    """Episodes plus the trace events the health tab is computed from.

    ``owner`` is the real account these episodes belong to; the user-facing
    console shows nothing that is not keyed to the signed-in user.
    """
    episode_ids = []
    # Keep the ORM objects: `Session.get()` cannot find a row that is still
    # pending (a pending object is not in the identity map until it flushes), so
    # looking episodes back up by id to set their event counts silently updated
    # nothing and every episode rendered as "0 events".
    episode_rows = {}
    # Spread across 30 days so before/after comparisons have both sides — and
    # run right up to the present, so the newest demo episodes head the evidence
    # list instead of sitting below whatever real conversations happened today.
    span = max(len(EPISODE_SCENARIOS) - 1, 1)
    for i, (objective, task_type, quality, verdict) in enumerate(EPISODE_SCENARIOS):
        eid = f"{PREFIX}ep-{i:02d}"
        created = ago(29.0 * (span - i) / span + 0.01)
        backfilled = i in (8, 11)  # a couple that cannot be replayed
        episode_rows[eid] = _add(
            db,
            EvolutionEpisode(
                episode_id=eid,
                run_id=f"{PREFIX}run-{i:02d}",
                message_id=f"{PREFIX}msg-{i:02d}",
                chat_id=f"{PREFIX}chat-{i % 5:02d}",
                user_id=owner,
                tenant_id="default",
                task_type=task_type,
                objective_hash=checksum(objective),
                objective_preview=objective,
                asset_bundle_id=None if backfilled else f"{PREFIX}bundle-{i:02d}",
                asset_bundle={} if backfilled else {"skills": ["ppt-design"], "prompt": "v3"},
                bundle_partial=False,
                backfilled=backfilled,
                outcome={"verdict": verdict, "attributed_to": "skill" if i % 3 == 0 else ""},
                quality_score=quality,
                cost_usd=round(0.004 + i * 0.0011, 4),
                latency_ms=2400 + i * 260,
                risk_result="pass",
                # Two kept private, so the user page can show the distinction it
                # makes between "contributed" and "kept to yourself" instead of
                # reporting the same number twice.
                privacy_class="private" if i in (5, 9) else "tenant",
                event_count=0,
                started_at=created,
                completed_at=created + timedelta(seconds=18),
                created_at=created,
            )
        )
        episode_ids.append((eid, created, verdict))

    # Trace events: offers (skill.selected) and opens (skill.opened) are what the
    # health tab counts, and the gap between them is the whole finding.
    seq_by_episode = {eid: 0 for eid, _, _ in episode_ids}
    event_no = 0
    for usage in SKILL_USAGE:
        skill = usage["skill"]
        for n in range(usage["offers"]):
            eid, created, _ = episode_ids[n % len(episode_ids)]
            seq_by_episode[eid] += 1
            event_no += 1
            db.add(
                EvolutionTraceEvent(
                    event_id=f"{PREFIX}evt-{event_no:05d}",
                    episode_id=eid,
                    message_id=f"{PREFIX}msg-{skill}-{n}",
                    seq=seq_by_episode[eid],
                    event_type="skill.selected",
                    actor_type="system",
                    payload={"selected": [skill], "degraded": False},
                    asset_kind="skill",
                    created_at=created,
                )
            )
        for n in range(usage["opens"]):
            # Opens land on episodes whose verdict produces the intended success
            # rate: the first `successes` opens sit on successful episodes.
            pool = [e for e in episode_ids if (e[2] == "success") == (n < usage["successes"])]
            eid, created, _ = (pool or episode_ids)[n % len(pool or episode_ids)]
            seq_by_episode[eid] += 1
            event_no += 1
            db.add(
                EvolutionTraceEvent(
                    event_id=f"{PREFIX}evt-{event_no:05d}",
                    episode_id=eid,
                    message_id=f"{PREFIX}msg-open-{skill}-{n}",
                    seq=seq_by_episode[eid],
                    event_type="skill.opened",
                    actor_type="model",
                    payload={"skill_id": skill, "tools_after_open": ["write_file", "bash"]},
                    asset_kind="skill",
                    created_at=created,
                )
            )

    # Memory events. The user page's "记忆" counter sums `injected` across these,
    # so without them that number stays at 0 even with a full evidence list.
    for i, (eid, created, _) in enumerate(episode_ids):
        seq_by_episode[eid] += 1
        event_no += 1
        db.add(
            EvolutionTraceEvent(
                event_id=f"{PREFIX}evt-mem-{event_no:05d}",
                episode_id=eid,
                message_id=f"{PREFIX}msg-mem-{i:02d}",
                seq=seq_by_episode[eid],
                event_type="memory.retrieved",
                actor_type="system",
                payload={
                    "injected": 1 + (i % 3),
                    "items": [
                        {
                            "layer": "fact",
                            "content_hash": checksum("mem", str(i)),
                            "workspace_id": "default",
                        }
                    ],
                },
                asset_kind="memory",
                created_at=created,
            )
        )

    for eid, count in seq_by_episode.items():
        episode_rows[eid].event_count = count
    db.commit()
    return [e[0] for e in episode_ids]


def _changes_for(kind: str, spec: dict, owner: str) -> list:
    """The executable payload behind a candidate.

    Skill candidates carry the document that would be installed; memory
    candidates carry the operations that would be applied. These are what the
    user's approval page renders — a candidate without them shows a diagnosis
    and an unexplained button.
    """
    if kind == "skill" and spec["operation"] in ("new", "patch", "merge"):
        return [{
            "tool_sequence": spec.get("tools", []),
            "document": {
                "display_name": spec["asset"],
                "description": spec.get("doc_desc", ""),
                "allowed_tools": spec.get("tools", []),
                "content": spec.get("doc_body", ""),
            },
        }]
    if kind == "memory":
        return [{
            "user_id": owner,
            "operations": spec.get("ops", []),
        }]
    return [{"tool_sequence": spec.get("tools", [])}]


def seed_candidates(db, episode_ids: list, owner: str) -> None:
    groups = (
        ("memory", MEMORY_CANDIDATES),
        ("skill", SKILL_CANDIDATES),
        ("agent_profile", ORCHESTRATION_CANDIDATES),
    )
    idx = 0
    for kind, specs in groups:
        for spec in specs:
            idx += 1
            cid = f"{PREFIX}cand-{kind}-{idx:02d}"
            approved = spec["status"] in ("active", "canary", "shadow", "replay_passed")
            db.add(
                EvolutionCandidate(
                    candidate_id=cid,
                    target_kind=kind,
                    target_asset_id=spec["asset"],
                    base_version_id="v1",
                    operation=spec["operation"],
                    ir={
                        "target_kind": kind,
                        "target_asset_id": spec["asset"],
                        "operation": spec["operation"],
                        "hypothesis": spec["hypothesis"],
                        # The change itself. Without this the approval row can
                        # only restate why something is proposed and never what
                        # it is, which is not enough to decide on.
                        "changes": _changes_for(kind, spec, owner),
                    },
                    hypothesis=spec["hypothesis"],
                    change_checksum=checksum(kind, spec["asset"], spec["operation"], str(idx)),
                    evidence_refs=episode_ids[: spec["evidence"]],
                    credit_decision={
                        "selected": spec["selected"],
                        "confidence": spec["confidence"],
                        "explanation": spec["hypothesis"][:60],
                    },
                    risk_tier=spec["risk"],
                    scope={"task_types": ["chat", "report"]} if kind == "agent_profile" else {},
                    status=spec["status"],
                    reject_reason=spec.get("reject_reason"),
                    proposer="evolution_cycle",
                    approved_by="console" if approved else None,
                    approved_at=ago(3) if approved else None,
                    created_at=ago(12 - idx * 0.4),
                )
            )
    db.commit()


def seed_releases(db) -> None:
    """Release board, including both rollback kinds.

    Skill releases are dated ≥30 days back because skill exposure age is read
    from here: the retirement rules refuse to judge anything younger than two
    weeks, so a demo release dated today would produce an empty health tab.
    """
    rows = [
        dict(rid="01", cid=f"{PREFIX}cand-skill-06", kind="skill", asset="excel-editing",
             stage="active", traffic=100, age=34),
        dict(rid="02", cid=f"{PREFIX}cand-skill-01", kind="skill", asset="industry-chain-mapping",
             stage="canary", traffic=25, age=31),
        dict(rid="03", cid=f"{PREFIX}cand-skill-02", kind="skill", asset="政策文件要点提取",
             stage="shadow", traffic=0, age=30),
        dict(rid="04", cid=f"{PREFIX}cand-skill-04", kind="skill", asset="capability-guide-brief",
             stage="active", traffic=100, age=46),
        dict(rid="05", cid=f"{PREFIX}cand-skill-05", kind="skill", asset="pdf-editing",
             stage="active", traffic=100, age=38),
        dict(rid="06", cid=f"{PREFIX}cand-skill-03", kind="skill", asset="ppt-design",
             stage="active", traffic=100, age=52),
        dict(rid="07", cid=f"{PREFIX}cand-skill-07", kind="skill", asset="word-editing",
             stage="active", traffic=100, age=41),
        dict(rid="08", cid=f"{PREFIX}cand-agent_profile-12", kind="agent_profile", asset="research",
             stage="active", traffic=100, age=9),
        dict(rid="09", cid=f"{PREFIX}cand-agent_profile-13", kind="agent_profile", asset="report",
             stage="canary", traffic=25, age=4),
        dict(rid="10", cid=f"{PREFIX}cand-memory-01", kind="memory", asset="mem:财务分析-主体核验",
             stage="active", traffic=100, age=6),
        dict(rid="11", cid=f"{PREFIX}cand-memory-02", kind="memory", asset="mem:周报口径-自然周",
             stage="canary", traffic=25, age=2),
        # Both rollback kinds: the overview counts them separately because a
        # guardrail rollback means the system worked, while a manual one means
        # the evaluation missed something.
        dict(rid="12", cid=f"{PREFIX}cand-agent_profile-15", kind="agent_profile", asset="code_exec",
             stage="rolled_back", traffic=0, age=7, rollback="guardrail",
             reason="灰度期 code_exec 成功率由 71% 降至 58%，触发护栏自动回滚"),
        dict(rid="13", cid=f"{PREFIX}cand-skill-05", kind="skill", asset="表格数据清洗",
             stage="rolled_back", traffic=0, age=15, rollback="manual",
             reason="人工观察到与 excel-editing 高度重复，选择在两者间摇摆，手动回滚"),
    ]
    for row in rows:
        db.add(
            EvolutionRelease(
                release_id=f"{PREFIX}rel-{row['rid']}",
                candidate_id=row["cid"],
                target_kind=row["kind"],
                target_asset_id=row["asset"],
                stage=row["stage"],
                traffic_percent=row["traffic"],
                scope_filter={},
                guardrails={"success_rate_floor": 0.6, "latency_p95_ms": 12000},
                rollback_version_id="v1" if row["stage"] != "shadow" else None,
                rolled_back_at=ago(1) if row.get("rollback") else None,
                rollback_reason=row.get("reason"),
                rollback_kind=row.get("rollback"),
                approved_by="console",
                created_at=ago(row["age"]),
                updated_at=ago(max(row["age"] - 2, 0)),
            )
        )
    db.commit()


def seed_profiles(db) -> None:
    specs = [
        dict(pid="research-v3", version="v3", tasks=["research"], active=True,
             cid=f"{PREFIX}cand-agent_profile-12",
             payload={"visible_tools": ["internet_search", "read_file", "write_file", "bash"],
                      "skill_candidates": 6, "memory_budget_ms": 900, "max_iterations": 12,
                      "note": "移除 8 个从未被调用的工具定义，约省 4200 tokens 上下文"}),
        dict(pid="report-v2", version="v2", tasks=["report"], active=True,
             cid=f"{PREFIX}cand-agent_profile-13",
             payload={"visible_tools": ["read_file", "write_file", "excel", "internet_search"],
                      "skill_candidates": 8, "memory_budget_ms": 900, "max_iterations": 10,
                      "note": "记忆检索预算 600ms→900ms，消除 23% 的超时无注入"}),
        dict(pid="chat-v2", version="v2", tasks=["chat"], active=False,
             cid=f"{PREFIX}cand-agent_profile-14",
             payload={"visible_tools": ["read_file", "write_file"], "skill_candidates": 6,
                      "memory_budget_ms": 600, "max_iterations": 8,
                      "note": "技能候选 12→6，灰度中；停用后回落内置装配"}),
    ]
    for spec in specs:
        db.add(
            EvolutionAgentProfile(
                profile_id=f"{PREFIX}{spec['pid']}",
                version=spec["version"],
                task_types=spec["tasks"],
                scope={"tenant_id": "default"},
                payload=spec["payload"],
                candidate_id=spec["cid"],
                is_active=spec["active"],
                created_by="evolution",
                created_at=ago(11),
                updated_at=ago(3),
            )
        )
    db.commit()


def seed_memory_ops(db, owner: str) -> None:
    specs = [
        dict(op="reweight", ref="ref-fin-subject", status="applied",
             reason="重复陈述 9 次但召回率 31%，提高检索权重",
             before={"weight": 1.0}, after={"weight": 1.6}),
        dict(op="deprecate", ref="ref-weekly-rolling7", status="applied",
             reason="与「自然周」口径冲突，且 30 天内 0 次复述",
             before={"active": True}, after={"active": False}),
        dict(op="merge", ref="ref-cite-source", status="applied",
             reason="3 条近重复合并为 1 条，释放 Top-K 名额",
             before={"count": 3}, after={"count": 1}),
        dict(op="reweight", ref="ref-ppt-confirm-first", status="reverted",
             reason="限定到 ppt 任务后误伤了 report 任务的召回，已撤销",
             before={"scope": "all"}, after={"scope": "ppt"}),
    ]
    for i, spec in enumerate(specs):
        db.add(
            EvolutionMemoryOp(
                op_id=f"{PREFIX}op-{i:02d}",
                candidate_id=f"{PREFIX}cand-memory-{i + 1:02d}",
                user_id=owner,
                workspace_id="default",
                operation=spec["op"],
                memory_ref=spec["ref"],
                external_id=f"{PREFIX}mem-{i:02d}",
                before_state=spec["before"],
                after_state=spec["after"],
                reason=spec["reason"],
                status=spec["status"],
                applied_at=ago(5 - i),
                reverted_at=ago(1) if spec["status"] == "reverted" else None,
            )
        )
    db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="只删除 demo- 数据，不写入")
    parser.add_argument("--user", default="", help="数据归属的用户 id（默认取最近活跃的真实用户）")
    args = parser.parse_args()

    with SessionLocal() as db:
        removed = clean(db)
        print("removed:", {k: v for k, v in removed.items() if v})
        if args.clean:
            return 0

        owner = resolve_user(db, args.user)
        print("owner:", owner)
        episode_ids = seed_episodes(db, owner)
        seed_candidates(db, episode_ids, owner)
        seed_releases(db)
        seed_profiles(db)
        seed_memory_ops(db, owner)

        print("seeded:")
        print("  episodes    ", db.query(EvolutionEpisode).count())
        print("  events      ", db.query(EvolutionTraceEvent).count())
        print("  candidates  ", db.query(EvolutionCandidate).count())
        print("  releases    ", db.query(EvolutionRelease).count())
        print("  profiles    ", db.query(EvolutionAgentProfile).count())
        print("  memory_ops  ", db.query(EvolutionMemoryOp).count())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
