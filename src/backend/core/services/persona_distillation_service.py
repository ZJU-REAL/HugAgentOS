"""Persona distillation service — lifecycle of persona-level distillation jobs (colleague skills / personal skills).

create_job → asyncio background run_job:
  map (per-session assemble_trajectory → session digest)
  → memory collection (colleague: all workspaces; personal: projects linked to the selected sessions)
  → reduce (synthesize SKILL.md)
  → colleague mirrors into admin_skill_drafts; personal waits for the user to confirm save.

Budget: each job consumes 1 daily run slot; every LLM call passes through the daily cost gate first;
once the per-job cost cap trips, we go into reduce early with the digests gathered so far (result marked partial).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.config.distillation import DistillationConfig, get_config
from core.db.engine import SessionLocal
from core.db.models import (
    AdminSkill,
    AdminSkillDraft,
    ChatSession,
    MemoryAudit,
    PersonaDistillJob,
    Project,
    TeamMember,
)
from core.infra.distillation_budget import (
    check_and_reserve_run,
    check_cost_budget,
    record_cost,
)
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.llm._distill_shared import skill_to_markdown
from core.llm.persona_distiller import distill_persona, summarize_session
from core.ontology.build_validator import ensure_ontology_build_valid
from core.services.distillation_service import assemble_trajectory

logger = logging.getLogger(__name__)

VALID_KINDS = ("colleague", "personal")

# High-confidentiality memories never enter the distillation corpus
_CONFIDENTIAL_VALUES = {"high", "secret", "confidential", "机密", "保密", "内部"}

_COLLEAGUE_NAME_RE = re.compile(r"^数字同事-([A-Z]+)$")


# ─────────────────────── Code-name based naming (A..Z, AA..) ────────────────────────


def _letters_to_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _num_to_letters(n: int) -> str:
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def allocate_colleague_identity(db: Session) -> Tuple[str, str]:
    """Allocate the next digital-colleague code name. Returns (display_name, skill_id).

    Scans display_name of existing skills and drafts, takes max ordinal +1 (carries over to AA after Z).
    """
    names: List[str] = []
    for (name,) in db.query(AdminSkill.display_name).all():
        if name:
            names.append(name)
    for (name,) in db.query(AdminSkillDraft.display_name).all():
        if name:
            names.append(name)
    max_n = 0
    for name in names:
        m = _COLLEAGUE_NAME_RE.match(name.strip())
        if m:
            max_n = max(max_n, _letters_to_num(m.group(1)))
    code = _num_to_letters(max_n + 1)
    return f"数字同事-{code}", f"colleague-{code.lower()}"


# ─────────────────────── Job creation / query ────────────────────────


def validate_chat_ids(db: Session, chat_ids: List[str], user_id: str) -> List[str]:
    """Verify that all chat_ids belong to user_id and are not deleted. Returns the list of invalid ids."""
    if not chat_ids:
        return []
    rows = (
        db.query(ChatSession.chat_id)
        .filter(
            ChatSession.chat_id.in_(chat_ids),
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
        )
        .all()
    )
    valid = {r[0] for r in rows}
    return [c for c in chat_ids if c not in valid]


def create_job(
    db: Session,
    *,
    kind: str,
    target_user_id: str,
    requested_by: str,
    scope: Dict[str, Any],
) -> PersonaDistillJob:
    cfg = get_config()
    if not cfg.persona_enabled:
        raise BadRequestError("人物技能蒸馏功能未启用（DISTILL_PERSONA_ENABLED=false）")
    if kind not in VALID_KINDS:
        raise BadRequestError(f"无效的蒸馏类型: {kind}")

    job = PersonaDistillJob(
        job_id=f"pdj_{uuid.uuid4().hex[:16]}",
        kind=kind,
        target_user_id=target_user_id,
        requested_by=requested_by,
        scope=scope or {},
        status="queued",
        created_at=datetime.utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def start_job_background(job_id: str) -> None:
    """Fire-and-forget background execution (routes call this from an async context)."""
    asyncio.create_task(run_job(job_id))


def job_to_dict(
    job: PersonaDistillJob,
    *,
    include_result: bool = False,
    admin_view: bool = False,
    draft: Optional[AdminSkillDraft] = None,
) -> dict:
    """PersonaDistillJob → API dict (shared by the lab and config routes).

    admin_view adds admin-console fields (target_user / mirrored-draft status); include_result carries
    the full SKILL.md text — a colleague draft may have been edited in AdminApp, so the draft's current value wins.
    """
    d = {
        "job_id": job.job_id,
        "kind": job.kind,
        "status": job.status,
        "progress_done": job.progress_done or 0,
        "progress_total": job.progress_total or 0,
        "cost_usd": float(job.cost_usd or 0),
        "scope": job.scope or {},
        "result_meta": job.result_meta or {},
        "saved_skill_id": job.saved_skill_id,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if admin_view:
        d["target_user_id"] = job.target_user_id
        d["result_draft_id"] = job.result_draft_id
        d["draft_review_status"] = draft.review_status if draft else None
    if include_result:
        d["result_skill_content"] = draft.skill_content if draft else job.result_skill_content
    return d


def get_job(db: Session, job_id: str) -> Optional[PersonaDistillJob]:
    return (
        db.query(PersonaDistillJob)
        .filter(PersonaDistillJob.job_id == job_id)
        .first()
    )


def list_jobs(
    db: Session,
    *,
    kind: Optional[str] = None,
    target_user_id: Optional[str] = None,
    requested_by: Optional[str] = None,
    limit: int = 50,
) -> List[PersonaDistillJob]:
    q = db.query(PersonaDistillJob)
    if kind:
        q = q.filter(PersonaDistillJob.kind == kind)
    if target_user_id:
        q = q.filter(PersonaDistillJob.target_user_id == target_user_id)
    if requested_by:
        q = q.filter(PersonaDistillJob.requested_by == requested_by)
    return q.order_by(PersonaDistillJob.created_at.desc()).limit(limit).all()


def cancel_job(db: Session, job_id: str) -> PersonaDistillJob:
    job = get_job(db, job_id)
    if job is None:
        raise ResourceNotFoundError("persona_distill_job", job_id)
    if job.status not in ("queued", "running"):
        raise BadRequestError(f"作业已结束（{job.status}），无法取消")
    job.status = "cancelled"
    job.finished_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


def delete_job(db: Session, job_id: str) -> None:
    job = get_job(db, job_id)
    if job is None:
        raise ResourceNotFoundError("persona_distill_job", job_id)
    if job.status == "running":
        raise BadRequestError("作业运行中，请先取消")
    db.delete(job)
    db.commit()


def recover_orphan_jobs() -> int:
    """After a process restart, mark orphaned running/queued jobs as failed (called from the startup hook)."""
    db = SessionLocal()
    try:
        rows = (
            db.query(PersonaDistillJob)
            .filter(PersonaDistillJob.status.in_(("queued", "running")))
            .all()
        )
        for job in rows:
            job.status = "failed"
            job.error = "backend restarted while job was in flight"
            job.finished_at = datetime.utcnow()
        if rows:
            db.commit()
        return len(rows)
    finally:
        db.close()


# ─────────────────────── Session-set resolution and sampling ────────────────────────


def _resolve_chat_ids(db: Session, job: PersonaDistillJob, cfg: DistillationConfig) -> Tuple[List[str], int]:
    """Resolve the session set covered by the job. Returns (chat_ids, total_candidates).

    Excludes automation virtual sessions and code_exec sessions; 'all' mode takes up to
    persona_max_sessions ordered by last_message_at descending (recent-first sampling).
    """
    scope = job.scope or {}
    chat_ids = scope.get("chat_ids")

    if isinstance(chat_ids, list) and chat_ids:
        ids = [str(c) for c in chat_ids]
        total = len(ids)
        if len(ids) > cfg.persona_max_sessions:
            ids = ids[: cfg.persona_max_sessions]
        return ids, total

    q = db.query(ChatSession.chat_id).filter(
        ChatSession.user_id == job.target_user_id,
        ChatSession.deleted_at.is_(None),
        or_(
            ChatSession.extra_data.is_(None),
            sa.and_(
                ~func.cast(ChatSession.extra_data, sa.Text).contains('"automation_run"'),
                ~func.cast(ChatSession.extra_data, sa.Text).contains('"code_exec_chat"'),
            ),
        ),
    )
    date_from = scope.get("date_from")
    date_to = scope.get("date_to")
    if date_from:
        q = q.filter(ChatSession.last_message_at >= date_from)
    if date_to:
        q = q.filter(ChatSession.last_message_at < date_to)

    total = q.count()
    rows = (
        q.order_by(ChatSession.last_message_at.desc().nullslast())
        .limit(cfg.persona_max_sessions)
        .all()
    )
    return [r[0] for r in rows], total


# ─────────────────────── Memory collection ────────────────────────


def _fact_confidential(item: dict) -> bool:
    meta = item.get("metadata") or {}
    conf = str(meta.get("confidentiality") or "").strip().lower()
    return conf in _CONFIDENTIAL_VALUES


def _slim_fact(item: dict) -> Dict[str, Any]:
    meta = item.get("metadata") or {}
    return {
        "memory": str(item.get("memory") or item.get("text") or "")[:500],
        "tags": meta.get("tags") or [],
    }


def _audit_memory_read(db: Session, *, actor: str, user_id: str, workspace_id: str, job_id: str) -> None:
    db.add(
        MemoryAudit(
            actor=actor,
            action="read",
            layer="batch",
            user_id=user_id,
            workspace_id=workspace_id[:64],
            reason=f"persona_distill:{job_id}",
        )
    )


async def _fetch_workspace_memories(
    scope_user_id: str,
    workspace_id: Optional[str],
    profile_user_id: Optional[str] = None,
) -> Tuple[str, List[dict]]:
    """Read one workspace's L1 profile (optional) and raw L2 entries; either failure is treated as empty."""
    from core.memory.profile import get as profile_get
    from core.memory.service import get_all_memories

    profile = ""
    if profile_user_id:
        try:
            profile = await profile_get(profile_user_id, workspace_id or "default")
        except Exception:
            profile = ""
    try:
        raw = await get_all_memories(scope_user_id, workspace_id=workspace_id)
    except Exception:
        raw = []
    return profile, raw


def _slim_facts(raw: List[dict], author_user_id: Optional[str] = None) -> List[dict]:
    """Confidentiality filtering + (optional) author filtering + slimming."""
    return [
        _slim_fact(it)
        for it in raw
        if not _fact_confidential(it)
        and (
            author_user_id is None
            or ((it.get("metadata") or {}).get("author_user_id")) == author_user_id
        )
    ]


async def collect_colleague_memories(
    db: Session,
    *,
    target_user_id: str,
    job_id: str,
    actor: str,
    include_personal: bool = True,
    include_project: bool = True,
) -> Dict[str, Any]:
    """Collect the target user's memories across all workspaces, grouped by workspace.

    - Default space: L1 profile + L2 facts (legacy data missing the workspace tag is treated as default)
    - Personal projects: per-project L1 + L2
    - Team projects: shared scope team:<tid>, keep only entries with author_user_id == target user;
      the team-shared L1 profile serves only as a project-background note
    Project workspaces are fetched in parallel; each workspace read writes one memory_audit row.
    """
    groups: Dict[str, Any] = {}

    if include_personal:
        profile, raw = await _fetch_workspace_memories(
            target_user_id, None, profile_user_id=target_user_id
        )
        facts = _slim_facts([
            it for it in raw
            if ((it.get("metadata") or {}).get("workspace_id") or "default") == "default"
        ])
        if profile or facts:
            groups["个人默认空间"] = {"profile": (profile or "")[:4000], "facts": facts[:80]}
        _audit_memory_read(db, actor=actor, user_id=target_user_id, workspace_id="default", job_id=job_id)

    if include_project:
        personal_projects = (
            db.query(Project)
            .filter(
                Project.kind == "personal",
                Project.owner_user_id == target_user_id,
                Project.deleted_at.is_(None),
            )
            .all()
        )
        team_ids = [
            r[0]
            for r in db.query(TeamMember.team_id)
            .filter(TeamMember.user_id == target_user_id)
            .all()
        ]
        team_projects = (
            db.query(Project)
            .filter(
                Project.kind == "team",
                Project.team_id.in_(team_ids),
                Project.deleted_at.is_(None),
            )
            .all()
        ) if team_ids else []

        # Each workspace is independent I/O; fetch in parallel
        results = await asyncio.gather(*(
            [
                _fetch_workspace_memories(
                    target_user_id, f"project:{p.project_id}", profile_user_id=target_user_id
                )
                for p in personal_projects
            ]
            + [
                _fetch_workspace_memories(f"team:{p.team_id}", f"project:{p.project_id}")
                for p in team_projects
            ]
        ))

        for p, (profile, raw) in zip(personal_projects, results[: len(personal_projects)]):
            facts = _slim_facts(raw)
            if profile or facts:
                groups[f"个人项目「{p.name}」"] = {
                    "profile": (profile or "")[:2000],
                    "facts": facts[:50],
                }
            _audit_memory_read(
                db, actor=actor, user_id=target_user_id,
                workspace_id=f"project:{p.project_id}", job_id=job_id,
            )
        for p, (_, raw) in zip(team_projects, results[len(personal_projects):]):
            facts = _slim_facts(raw, author_user_id=target_user_id)
            if facts:
                groups[f"团队项目「{p.name}」（仅本人撰写条目）"] = {"facts": facts[:50]}
            _audit_memory_read(
                db, actor=actor, user_id=target_user_id,
                workspace_id=f"project:{p.project_id}", job_id=job_id,
            )

    db.commit()
    return groups


async def collect_personal_project_memories(
    db: Session,
    *,
    user_id: str,
    chat_ids: List[str],
    job_id: str,
) -> Dict[str, Any]:
    """personal mode: L2 facts authored by the user, from projects the selected sessions are attached to, as auxiliary corpus."""
    if not chat_ids:
        return {}
    project_ids = {
        r[0]
        for r in db.query(ChatSession.project_id)
        .filter(ChatSession.chat_id.in_(chat_ids), ChatSession.project_id.isnot(None))
        .all()
        if r[0]
    }
    if not project_ids:
        return {}

    groups: Dict[str, Any] = {}
    projects = (
        db.query(Project)
        .filter(Project.project_id.in_(project_ids), Project.deleted_at.is_(None))
        .all()
    )
    results = await asyncio.gather(*(
        _fetch_workspace_memories(
            f"team:{p.team_id}" if (p.kind == "team" and p.team_id) else user_id,
            f"project:{p.project_id}",
        )
        for p in projects
    ))
    for p, (_, raw) in zip(projects, results):
        facts = _slim_facts(raw, author_user_id=user_id if p.kind == "team" else None)
        if facts:
            groups[f"项目「{p.name}」"] = {"facts": facts[:50]}
        _audit_memory_read(
            db, actor=user_id, user_id=user_id,
            workspace_id=f"project:{p.project_id}", job_id=job_id,
        )
    db.commit()
    return groups


# ─────────────────────── Background execution ────────────────────────


def _job_cancelled(db: Session, job_id: str) -> bool:
    db.expire_all()
    row = (
        db.query(PersonaDistillJob.status)
        .filter(PersonaDistillJob.job_id == job_id)
        .first()
    )
    return bool(row and row[0] == "cancelled")


async def run_job(job_id: str) -> None:
    db = SessionLocal()
    try:
        await _run_job_inner(db, job_id)
    except Exception as exc:  # safety net: any uncaught exception lands the job in failed
        logger.exception("persona_distill: job %s crashed", job_id)
        try:
            db.rollback()
            job = get_job(db, job_id)
            if job and job.status in ("queued", "running"):
                job.status = "failed"
                job.error = str(exc)[:2000]
                job.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            logger.exception("persona_distill: job %s failed to persist error", job_id)
    finally:
        db.close()


async def _run_job_inner(db: Session, job_id: str) -> None:
    cfg = get_config()
    job = get_job(db, job_id)
    if job is None or job.status != "queued":
        return
    job.status = "running"
    job.started_at = datetime.utcnow()
    db.commit()

    # Each job consumes 1 daily run slot (map's many lightweight calls are not counted again)
    allowed, reason = await check_and_reserve_run(cfg)
    if not allowed:
        job.status = "failed"
        job.error = f"budget: {reason}"
        job.finished_at = datetime.utcnow()
        db.commit()
        return

    scope = job.scope or {}
    chat_ids, total_candidates = _resolve_chat_ids(db, job, cfg)
    if not chat_ids:
        job.status = "failed"
        job.error = "没有可蒸馏的会话"
        job.finished_at = datetime.utcnow()
        db.commit()
        return

    sampled_ratio = (len(chat_ids) / total_candidates) if total_candidates else 1.0
    job.progress_total = len(chat_ids)
    db.commit()

    # ── map: per-session digest (concurrency bounded by persona_map_concurrency) ──
    hint = str(scope.get("hint") or "")
    total_cost = 0.0
    partial = False
    digests: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(max(1, cfg.persona_map_concurrency))
    cost_lock = asyncio.Lock()

    async def _one(chat_id: str) -> Optional[Dict[str, Any]]:
        nonlocal total_cost, partial
        async with sem:
            async with cost_lock:
                if partial:
                    return None
                if total_cost >= cfg.persona_job_max_cost_usd:
                    partial = True
                    return None
            ok, _reason = await check_cost_budget(cfg)
            if not ok:
                async with cost_lock:
                    partial = True
                return None
            # assemble + LLM (assemble is a synchronous DB read; the volume is small enough to accept)
            traj = assemble_trajectory(db, chat_id, cfg)
            if traj is None or not traj.turns:
                return None
            try:
                digest, cost = await summarize_session(traj, cfg, hint=hint)
            except Exception as exc:
                logger.warning("persona_distill: digest failed chat=%s (%s)", chat_id, exc)
                return None
            async with cost_lock:
                total_cost += cost
            await record_cost(cost)
            return digest

    done = 0
    # Run in batches, so progress persists to DB and cancellation checks can happen
    batch_size = max(1, cfg.persona_map_concurrency) * 2
    for i in range(0, len(chat_ids), batch_size):
        if _job_cancelled(db, job_id):
            return
        batch = chat_ids[i : i + batch_size]
        results = await asyncio.gather(*(_one(c) for c in batch))
        digests.extend(d for d in results if d)
        done += len(batch)
        job = get_job(db, job_id)
        if job is None or job.status == "cancelled":
            return
        job.progress_done = min(done, job.progress_total)
        job.cost_usd = round(total_cost, 4)
        job.intermediate = list(digests)  # only a new object triggers JSON column dirty detection
        db.commit()
        if partial:
            break

    useful = [d for d in digests if not d.get("low_value")]
    if not useful:
        job = get_job(db, job_id)
        if job and job.status == "running":
            job.status = "failed"
            job.error = "所选会话无有效信息量（摘要全部为空/低价值）"
            job.cost_usd = round(total_cost, 4)
            job.finished_at = datetime.utcnow()
            db.commit()
        return

    # ── Memory collection ──
    memories: Dict[str, Any] = {}
    try:
        if job.kind == "colleague":
            memories = await collect_colleague_memories(
                db,
                target_user_id=job.target_user_id,
                job_id=job_id,
                actor=job.requested_by,
                include_personal=bool(scope.get("include_memories", True)),
                include_project=bool(scope.get("include_project_memories", True)),
            )
        else:
            if scope.get("include_project_memories", True):
                memories = await collect_personal_project_memories(
                    db, user_id=job.target_user_id, chat_ids=chat_ids, job_id=job_id
                )
    except Exception as exc:
        logger.warning("persona_distill: memory collection failed for %s (%s)", job_id, exc)

    # ── Naming ──
    if job.kind == "colleague":
        display_name, skill_id = allocate_colleague_identity(db)
        assigned_identity = f"display_name: {display_name}\nskill_id: {skill_id}"
    else:
        display_name, skill_id = "", ""
        assigned_identity = ""

    if _job_cancelled(db, job_id):
        return

    # ── reduce ──
    try:
        from core.ontology.validator import render_runtime_prompt
        from core.services.ontology_service import build_user_ontology_runtime

        ontology_task = "\n".join(
            filter(
                None,
                [hint, json.dumps(digests[:5], ensure_ascii=False, default=str)[:8000]],
            )
        )
        _, ontology_runtime = build_user_ontology_runtime(
            user_id=str(job.target_user_id),
            task=ontology_task,
            db=db,
        )
        ontology_context = render_runtime_prompt(ontology_runtime)
        if ontology_context:
            hint = "\n\n".join(
                filter(
                    None,
                    [
                        hint,
                        "生成的技能草稿必须使用规范领域术语并满足完整工作流：\n"
                        + ontology_context,
                    ],
                )
            )
    except Exception as exc:  # The materialization gate still validates the draft.
        logger.debug("persona_distill: ontology bootstrap unavailable (%s)", exc)

    effective_ratio = sampled_ratio * (len(digests) / len(chat_ids) if chat_ids else 1.0)
    out = await distill_persona(
        job.kind,
        digests,
        memories or None,
        assigned_identity,
        hint,
        round(effective_ratio, 2),
        cfg,
    )
    total_cost += out.cost_usd
    await record_cost(out.cost_usd)

    job = get_job(db, job_id)
    if job is None or job.status == "cancelled":
        return
    job.cost_usd = round(total_cost, 4)

    if out.skill is None:
        job.status = "failed"
        job.error = f"reduce 阶段失败: {out.error}"
        job.finished_at = datetime.utcnow()
        db.commit()
        return

    # Naming finalization: colleague is forced to use the allocated code name (model output is not trusted).
    # The code name is allocated before reduce (the prompt needs it), and reduce takes tens of seconds —
    # a concurrent job may have consumed the same code in the meantime, so re-check before persisting;
    # if taken, advance to the next code and replace the old code throughout the body text.
    fm = out.skill.get("frontmatter") or {}
    if job.kind == "colleague":
        final_display, final_sid = allocate_colleague_identity(db)
        if final_sid != skill_id:
            body = out.skill.get("instructions_md") or ""
            out.skill["instructions_md"] = (
                body.replace(display_name, final_display).replace(skill_id, final_sid)
            )
            out.digest_text = out.digest_text.replace(display_name, final_display)
            display_name, skill_id = final_display, final_sid
        out.skill["id"] = skill_id
        fm["name"] = skill_id
        fm["display_name"] = display_name
    else:
        sid = str(out.skill.get("id") or fm.get("name") or f"personal-{uuid.uuid4().hex[:8]}")
        sid = re.sub(r"[^a-z0-9_-]+", "-", sid.lower()).strip("-") or f"personal-{uuid.uuid4().hex[:8]}"
        out.skill["id"] = sid
        fm["name"] = sid
        fm.setdefault("display_name", sid)
    out.skill["frontmatter"] = fm

    content = skill_to_markdown(out.skill)
    job.result_skill_content = content
    job.result_meta = {
        "proposed_skill_id": out.skill["id"],
        "display_name": str(fm.get("display_name") or out.skill["id"]),
        "description": str(fm.get("description") or ""),
        "tags": list(fm.get("tags") or []),
        "confidence": out.confidence,
        "digest_text": out.digest_text,
        "sampled_ratio": round(effective_ratio, 2),
        "partial": partial,
        "session_count": len(chat_ids),
        "useful_digests": len(useful),
    }

    # colleague: mirror the draft into admin_skill_drafts (unified auditing/review)
    if job.kind == "colleague":
        draft_id = f"dsk_{uuid.uuid4().hex[:16]}"
        db.add(
            AdminSkillDraft(
                draft_id=draft_id,
                proposed_skill_id=out.skill["id"],
                decision="new_skill",
                display_name=str(fm.get("display_name") or out.skill["id"]),
                description=str(fm.get("description") or ""),
                tags=list(fm.get("tags") or []),
                allowed_tools=list(fm.get("allowed_tools") or []),
                version=str(fm.get("version") or "0.1.0"),
                skill_content=content,
                extra_files={},
                source_chat_id=chat_ids[0] if chat_ids else job.job_id,
                source_user_id=job.target_user_id,
                source_trace_ids=chat_ids,
                trajectory_digest=(
                    f"人物蒸馏（{len(chat_ids)} 个会话，采样比例 {effective_ratio:.2f}）\n"
                    f"画像摘要：{out.digest_text}"
                ),
                distillation_cost_usd=round(total_cost, 4),
                draft_kind="colleague",
                source_job_id=job.job_id,
                review_status="pending",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        job.result_draft_id = draft_id

    job.status = "completed"
    job.finished_at = datetime.utcnow()
    db.commit()
    logger.info(
        "persona_distill: job %s completed kind=%s sessions=%d cost=%.4f",
        job_id, job.kind, len(chat_ids), total_cost,
    )


# ─────────────────────── Persisting personal-job output ────────────────────────


def save_personal_skill(
    db: Session,
    job: PersonaDistillJob,
    *,
    edited_content: Optional[str] = None,
    enable: bool = True,
) -> AdminSkill:
    """Persist a personal job's output as the user's private skill (owner=target_user_id).

    On skill_id collision a numeric suffix is appended automatically; colliding with a public
    or someone else's skill likewise switches the id instead of overwriting.
    """
    from core.agent_skills.registry import _load_skill_metadata_from_str

    if job.status != "completed" or not job.result_skill_content:
        raise BadRequestError("作业尚未完成或无产物")
    if job.saved_skill_id:
        raise BadRequestError(f"产物已保存为技能 {job.saved_skill_id}")

    content = (edited_content or job.result_skill_content).strip()
    try:
        meta = _load_skill_metadata_from_str(
            content, (job.result_meta or {}).get("proposed_skill_id") or "personal-skill"
        )
    except Exception as exc:
        raise BadRequestError(f"SKILL.md 解析失败：{exc}")

    base_id = meta.id
    taken = {
        r[0]
        for r in db.query(AdminSkill.skill_id)
        .filter(or_(AdminSkill.skill_id == base_id, AdminSkill.skill_id.like(f"{base_id}-%")))
        .all()
    }
    skill_id = base_id
    for i in range(2, 100):
        if skill_id not in taken:
            break
        skill_id = f"{base_id}-{i}"
    else:
        raise BadRequestError("无法分配可用的技能 ID")

    if skill_id != meta.id:
        # Also rewrite the name in the content frontmatter to keep it consistent with the id
        content = re.sub(
            rf"^name:\s*{re.escape(meta.id)}\s*$",
            f"name: {skill_id}",
            content,
            count=1,
            flags=re.MULTILINE,
        )

    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=meta.name or skill_id,
        description=meta.description or "",
        instructions=content,
        tool_names=list(meta.allowed_tools or []),
        ontology_tags=list(meta.tags or []),
    )

    skill = AdminSkill(
        skill_id=skill_id,
        skill_content=content,
        display_name=meta.name,
        description=meta.description,
        version=meta.version or "0.1.0",
        tags=list(meta.tags or []),
        allowed_tools=list(meta.allowed_tools or []),
        extra_files={},
        dependencies={},
        is_enabled=bool(enable),
        owner_user_id=str(job.target_user_id),
        created_by="personal_distill",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(skill)
    job.saved_skill_id = skill_id
    if edited_content:
        job.result_skill_content = content
    db.commit()
    db.refresh(skill)

    try:
        from core.agent_skills.cache_refresh import refresh_skill_caches
        refresh_skill_caches()
    except Exception as exc:
        logger.warning("persona_distill: cache refresh failed after save (%s)", exc)
    return skill
