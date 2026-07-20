"""Human-governed ontology evolution from runtime evidence.

The service only creates candidates and draft pack versions.  It never changes
an active-version pointer; activation remains an explicit administrator action.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import uuid
from collections import defaultdict
from threading import Lock
from typing import Any

from core.db.models import ChatMessage, OntologyEnforcementEvent
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.memory.sanitizer import sanitize
from core.services.ontology_service import OntologyService
from sqlalchemy.orm import Session

_SECTIONS = {"term": "concepts", "relation": "relations", "constraint": "constraints"}
logger = logging.getLogger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_SCHEDULE_LOCK = Lock()
_EVOLUTION_RUNNING = False


def schedule_ontology_evolution(*, user_id: str) -> bool:
    """Debounce the low-priority evidence prefilter on the current event loop."""
    global _EVOLUTION_RUNNING
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    with _SCHEDULE_LOCK:
        if _EVOLUTION_RUNNING:
            return False
        _EVOLUTION_RUNNING = True

    async def _run() -> None:
        global _EVOLUTION_RUNNING
        from core.db.engine import SessionLocal

        db = SessionLocal()
        try:
            await OntologyEvolutionService(db).generate_candidates(
                min_occurrences=3,
                limit=500,
                user_id=user_id or "system",
            )
        except Exception:  # noqa: BLE001 - a background learner must never affect delivery
            logger.warning("ontology evolution background pass failed", exc_info=True)
        finally:
            db.close()
            with _SCHEDULE_LOCK:
                _EVOLUTION_RUNNING = False

    task = loop.create_task(_run(), name="ontology-evolution-prefilter")
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return True


def _parse_json_array_lenient(text: str) -> list[Any] | None:
    """Parse a JSON array without coupling the service layer to orchestration."""
    if not text:
        return None
    candidates = [text.strip()]
    fenced = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
    return None


class OntologyEvolutionService:
    def __init__(self, db: Session):
        self.db = db
        self.ontology = OntologyService(db)

    async def generate_candidates(
        self,
        *,
        min_occurrences: int = 3,
        limit: int = 500,
        user_id: str = "system",
        model_name: str | None = None,
    ) -> dict[str, Any]:
        rows = (
            self.db.query(OntologyEnforcementEvent)
            .filter(
                OntologyEnforcementEvent.pack_id.isnot(None),
                OntologyEnforcementEvent.decision.in_(("deny", "revise", "escalate")),
            )
            .order_by(OntologyEnforcementEvent.created_at.desc())
            .limit(limit)
            .all()
        )
        groups: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
        for row in rows:
            key = (row.pack_id or "", row.rule_id or "", row.target or "")
            groups[key].append(row)
        recurring = [
            (key, items)
            for key, items in groups.items()
            if key[0] and len(items) >= min_occurrences
        ]
        if not recurring:
            return {"created": 0, "prefiltered_groups": 0, "items": []}

        chat_ids = list(
            dict.fromkeys(row.chat_id for _, items in recurring for row in items if row.chat_id)
        )[:50]
        messages_by_chat: dict[str, list[dict[str, str]]] = defaultdict(list)
        if chat_ids:
            message_rows = (
                self.db.query(ChatMessage)
                .filter(ChatMessage.chat_id.in_(chat_ids))
                .order_by(ChatMessage.created_at.desc())
                .limit(300)
                .all()
            )
            for message in message_rows:
                bucket = messages_by_chat[str(message.chat_id)]
                if len(bucket) < 6 and message.role in {"user", "assistant"}:
                    bucket.append(
                        {
                            "role": str(message.role),
                            "content": str(message.content or "")[:500],
                        }
                    )

        summaries = [
            {
                "pack_id": key[0],
                "rule_id": key[1],
                "target": key[2],
                "occurrences": len(items),
                "event_ids": [item.event_id for item in items[:20]],
                "samples": [(item.details or {}).get("violations", []) for item in items[:3]],
                "conversation_samples": [
                    {"chat_id": chat_id, "messages": list(reversed(messages_by_chat[chat_id]))}
                    for chat_id in dict.fromkeys(
                        str(item.chat_id) for item in items if item.chat_id
                    )
                    if messages_by_chat.get(chat_id)
                ][:3],
            }
            for key, items in recurring
        ]
        sanitized_summaries = [
            item
            for summary in summaries
            if isinstance((item := self._sanitize_json_value(summary)), dict)
        ]
        if not sanitized_summaries:
            return {"created": 0, "prefiltered_groups": len(recurring), "items": []}
        summaries = sanitized_summaries
        proposals = await self._llm_proposals(
            sanitized_summaries,
            user_id=user_id,
            model_name=model_name,
        )
        created = []
        for summary in summaries:
            matching = [
                item
                for item in proposals
                if item.get("pack_id") == summary["pack_id"]
                and item.get("source_rule_id", "") == summary["rule_id"]
            ]
            if not matching:
                matching = [
                    {
                        "pack_id": summary["pack_id"],
                        "source_rule_id": summary["rule_id"],
                        "candidate_type": "false_positive",
                        "proposal": {
                            "operation": "review_rule",
                            "rule_id": summary["rule_id"],
                            "reason": "同一门禁规则在近期重复触发，需人工判断规则过严或任务策略有误。",
                        },
                        "evidence": [
                            f"{summary['occurrences']} 次重复触发，目标 {summary['target']}"
                        ],
                    }
                ]
            for proposal in matching:
                sanitized = self._sanitize_proposal(proposal, summary)
                if sanitized is None or self._already_drafted(summary["event_ids"]):
                    continue
                row = self.ontology.repo.create_draft(
                    {
                        "draft_id": f"ontod_{uuid.uuid4().hex[:16]}",
                        "pack_id": sanitized["pack_id"],
                        "source_type": "enforcement",
                        "candidate_type": sanitized["candidate_type"],
                        "proposal": sanitized["proposal"],
                        "evidence": sanitized["evidence"],
                        "source_event_ids": summary["event_ids"],
                        "value_score": min(100, 40 + summary["occurrences"] * 10),
                        "review_status": "pending",
                    }
                )
                created.append(row.draft_id)
        return {
            "created": len(created),
            "prefiltered_groups": len(recurring),
            "items": created,
        }

    def ingest_user_correction(
        self,
        *,
        user_id: str,
        chat_id: str,
        message_id: str,
        feedback_id: str,
        comment: str,
    ) -> list[str]:
        """Turn explicit, domain-matched user correction into human-review drafts.

        The user's text is evidence, not an ontology mutation instruction.  It
        therefore enters the queue as a diagnostic candidate and can never be
        materialized or activated automatically.
        """
        from core.db.models import ChatMessage
        from core.services.ontology_service import build_user_ontology_runtime

        assistant = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.message_id == message_id,
                ChatMessage.chat_id == chat_id,
                ChatMessage.role == "assistant",
            )
            .first()
        )
        if assistant is None:
            return []
        prompt_row = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.role == "user",
                ChatMessage.created_at <= assistant.created_at,
            )
            .order_by(ChatMessage.created_at.desc())
            .first()
        )
        task = prompt_row.content if prompt_row else comment
        opted_in, runtime = build_user_ontology_runtime(
            user_id=user_id,
            task=task,
            db=self.db,
        )
        if not opted_in:
            return []
        source_id = f"feedback:{feedback_id}"
        if self._already_drafted([source_id]):
            return []
        evidence_texts: list[str] = []
        for text in (
            f"用户纠正：{comment[:500]}",
            f"原任务：{task[:500]}",
            f"原回答：{assistant.content[:500]}",
        ):
            result = sanitize(text)
            if result.reject:
                return []
            evidence_texts.append(result.text or "")
        created: list[str] = []
        for pack in runtime.get("packs", []):
            if not pack.get("workflows"):
                continue
            draft = self.ontology.repo.create_draft(
                {
                    "draft_id": f"ontod_{uuid.uuid4().hex[:16]}",
                    "pack_id": pack["pack_id"],
                    "source_type": "user_correction",
                    "candidate_type": "false_positive",
                    "proposal": {
                        "operation": "review_rule",
                        "reason": "用户对本体约束场景的回答给出显式纠正，需人工判断规则、术语或证据要求是否调整。",
                        "message_id": message_id,
                    },
                    "evidence": evidence_texts,
                    "source_event_ids": [source_id],
                    "value_score": 90,
                    "review_status": "pending",
                }
            )
            created.append(draft.draft_id)
        return created

    def materialize_approved_draft(self, draft_id: str):
        draft = self.ontology.repo.get_draft(draft_id)
        if not draft:
            raise ResourceNotFoundError("ontology_draft", draft_id)
        if draft.review_status != "approved":
            raise BadRequestError("只有人工审核通过的演进草案才能物化为新版本")
        proposal = draft.proposal or {}
        if proposal.get("materialized_version_id"):
            raise BadRequestError("该演进草案已经物化，不能重复生成版本")
        operation = proposal.get("operation")
        section = proposal.get("section")
        value = proposal.get("value")
        if (
            operation not in {"add", "replace"}
            or section not in {"concepts", "relations", "constraints", "workflows"}
            or not isinstance(value, dict)
        ):
            raise BadRequestError("该草案是诊断建议，不能直接物化；请在 Domain Pack 编辑器中处理")
        pack = self.ontology.repo.get_pack(draft.pack_id)
        if not pack or not pack.active_version_id:
            raise BadRequestError("Domain Pack 没有可作为基线的激活版本")
        working_draft = self.ontology.repo.get_working_draft(draft.pack_id)
        baseline = working_draft or self.ontology.repo.get_version(pack.active_version_id)
        if not baseline:
            raise BadRequestError("找不到激活版本内容")
        document = copy.deepcopy(baseline.content)
        items = list(document.get(section) or [])
        item_id = str(value.get("id") or "")
        index = next((i for i, item in enumerate(items) if item.get("id") == item_id), None)
        if operation == "add":
            if index is not None:
                raise BadRequestError(f"{section} 中已存在 id={item_id}")
            items.append(value)
        else:
            if index is None:
                raise BadRequestError(f"{section} 中不存在待替换的 id={item_id}")
            items[index] = value
        document[section] = items
        if working_draft is None:
            document["version"] = self._next_patch_version(
                draft.pack_id,
                str(document["version"]),
            )
        version, _ = self.ontology.save_working_draft(
            draft.pack_id,
            document,
            draft_version_id=working_draft.version_id if working_draft else None,
            expected_checksum=working_draft.checksum if working_draft else None,
            actor_id="ontology_evolution",
        )
        draft.reviewer_comment = "\n".join(
            filter(None, [draft.reviewer_comment or "", f"已合并到工作草稿 {version.version}"])
        )
        draft.proposal = {**proposal, "materialized_version_id": version.version_id}
        self.db.commit()
        return version

    async def _llm_proposals(
        self,
        summaries: list[dict[str, Any]],
        *,
        user_id: str,
        model_name: str | None,
    ) -> list[dict[str, Any]]:
        prompt = (
            "你是 Domain Pack 演进候选生成器。输入是已经过去重、达到重复阈值的门禁证据。"
            "只提出候选，不得声称激活或自动修改本体。优先判断 false_positive；只有证据充分时"
            "才提出 term/relation/constraint。禁止 OWL、代码和外部链接。\n\n"
            f"证据组：\n{json.dumps(summaries, ensure_ascii=False)[:16000]}\n\n"
            "严格输出 JSON 数组。每项："
            '{"pack_id":"...","source_rule_id":"...",'
            '"candidate_type":"term|relation|constraint|false_positive",'
            '"proposal":{"operation":"add|replace|review_rule",'
            '"section":"concepts|relations|constraints|workflows","value":{}},'
            '"evidence":["只引用输入证据"]}。'
        )
        try:
            from orchestration.subagents.ontology_reviewer import _run_text_agent

            text = await _run_text_agent(
                prompt,
                user_id=user_id,
                model_name=model_name,
                model_provider_id=None,
                runtime={"enabled": False, "packs": [], "review_level": "none"},
            )
            return [
                item for item in (_parse_json_array_lenient(text) or []) if isinstance(item, dict)
            ]
        except Exception:
            return []

    def _sanitize_proposal(
        self,
        raw: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any] | None:
        candidate_type = raw.get("candidate_type")
        if candidate_type not in {"term", "relation", "constraint", "false_positive"}:
            return None
        pack_id = str(raw.get("pack_id") or "")
        if pack_id != summary["pack_id"] or not self.ontology.repo.get_pack(pack_id):
            return None
        proposal = raw.get("proposal")
        if not isinstance(proposal, dict):
            return None
        sanitized_proposal = self._sanitize_json_value(proposal)
        if not isinstance(sanitized_proposal, dict):
            return None
        proposal = sanitized_proposal
        operation = proposal.get("operation")
        if operation not in {"add", "replace", "review_rule"}:
            return None
        if operation != "review_rule":
            expected_section = _SECTIONS.get(candidate_type)
            if proposal.get("section") != expected_section or not isinstance(
                proposal.get("value"), dict
            ):
                return None
            # Validate the entire proposed document before it reaches human review.
            pack = self.ontology.repo.get_pack(pack_id)
            baseline = self.ontology.repo.get_version(pack.active_version_id) if pack else None
            if baseline:
                candidate = copy.deepcopy(baseline.content)
                items = list(candidate.get(expected_section) or [])
                value = proposal["value"]
                idx = next(
                    (i for i, item in enumerate(items) if item.get("id") == value.get("id")), None
                )
                if operation == "add" and idx is None:
                    items.append(value)
                elif operation == "replace" and idx is not None:
                    items[idx] = value
                else:
                    return None
                candidate[expected_section] = items
                _, report = self.ontology.validate_document(candidate)
                if not report.get("valid"):
                    return None
        evidence: list[str] = []
        for item in (raw.get("evidence") or [])[:10]:
            result = sanitize(str(item)[:500])
            if result.reject:
                return None
            evidence.append(result.text or "")
        return {
            "pack_id": pack_id,
            "candidate_type": candidate_type,
            "proposal": proposal,
            "evidence": evidence,
        }

    def _already_drafted(self, source_event_ids: list[str]) -> bool:
        source = set(source_event_ids)
        return any(
            source & set(row.source_event_ids or [])
            for row in self.ontology.repo.list_drafts(limit=500)
        )

    def _next_patch_version(self, pack_id: str, current: str) -> str:
        core = current.split("+", 1)[0].split("-", 1)[0]
        major, minor, patch = (int(part) for part in core.split("."))
        while True:
            patch += 1
            candidate = f"{major}.{minor}.{patch}"
            if self.ontology.repo.get_pack_version(pack_id, candidate) is None:
                return candidate

    @staticmethod
    def _sanitize_json_value(value: Any) -> Any | None:
        """Redact PII/secrets and reject classified material before the draft pipeline."""
        try:
            encoded = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
        result = sanitize(encoded)
        if result.reject or result.text is None:
            return None
        try:
            return json.loads(result.text)
        except (json.JSONDecodeError, TypeError):
            return None
