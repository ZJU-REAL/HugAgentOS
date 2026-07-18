"""Internal API consumed by the ``batch_runner`` MCP server (stdio subprocess).

The MCP server lives in a separate process and cannot touch the DB
directly. Instead it HTTP-calls back into this endpoint with a service
token to:
  - parse uploaded files (xlsx/word) → structured items
  - split natural-language enumeration → items via LLM call
  - infer a default prompt template via LLM call
  - persist the BatchPlan row
  - return ``{plan_id, total, preview, default_template, ...}`` to the MCP tool

The token is checked against ``BACKEND_INTERNAL_TOKEN`` env var; if unset,
the endpoint is open (dev convenience). In production set the env var.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.services.model_config import ModelConfigService
from core.db.engine import SessionLocal
from core.db.models import Artifact, BatchPlan
from core.llm.message_compat import strip_thinking

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/internal/batch", tags=["internal-batch"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ResolveBody(BaseModel):
    instruction: str = ""
    file_ids: List[str] = []
    text_items: List[str] = []
    chat_id: str = ""
    user_id: Optional[str] = None  # for backend-to-MCP plan attribution; optional


# ---------------------------------------------------------------------------
# Auth (minimal: shared secret env var)
# ---------------------------------------------------------------------------


def _check_internal_token(token: Optional[str]) -> None:
    expected = os.environ.get("BACKEND_INTERNAL_TOKEN", "")
    if not expected:
        # Never allow this to run unauthenticated in production: if no token is
        # configured, reject outright (fail-closed), so this internal endpoint
        # (which calls the LLM, reads arbitrary Artifacts by file_id, and writes
        # to the DB using the passed-in user_id) isn't left exposed on misconfig.
        # Only skip the check in non-production (dev) for convenient local testing.
        from core.config.settings import settings

        if settings.server.is_prod:
            raise HTTPException(
                status_code=503,
                detail="internal endpoint disabled: BACKEND_INTERNAL_TOKEN not configured",
            )
        return
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid internal token")


# ---------------------------------------------------------------------------
# LLM helper (no agentscope dependency — direct httpx to OpenAI-compatible API)
# ---------------------------------------------------------------------------


async def _call_llm(prompt: str, *, system: str = "", max_tokens: int = 1500) -> str:
    """One-shot call to the configured main_agent model. Returns response text.

    For thinking-style models (DeepSeek R1, qwen3-thinking) we ask the
    backend to disable thinking via ``chat_template_kwargs.enable_thinking``
    when supported — otherwise we strip ``<think>...</think>`` blocks from
    the response post-hoc.
    """
    cfg = ModelConfigService.get_instance().resolve("main_agent")
    if cfg is None:
        base_url = os.environ.get("MODEL_URL", "")
        api_key = os.environ.get("API_KEY", "")
        model_name = os.environ.get("BASE_MODEL_NAME", "")
        timeout = 60
    else:
        base_url = cfg.base_url
        api_key = cfg.api_key
        model_name = cfg.model_name
        timeout = cfg.timeout

    if not base_url or not model_name:
        raise RuntimeError("main_agent model not configured")

    messages: List[Dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    base_payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "stream": False,
    }
    payload_with_extra = {
        **base_payload,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Try with extra_body first (vLLM / qwen3 / DeepSeek-compatible);
        # fall back to plain payload if endpoint rejects unknown fields.
        resp = await client.post(url, json=payload_with_extra, headers=headers)
        if resp.status_code >= 400:
            resp = await client.post(url, json=base_payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    try:
        raw = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected LLM response shape: {data}") from exc
    return strip_thinking(raw).strip()


# ---------------------------------------------------------------------------
# Source resolvers
# ---------------------------------------------------------------------------


async def _resolve_text_items(instruction: str, text_items: List[str]) -> List[Dict[str, Any]]:
    """Convert a list of strings → items list. If text_items empty, fall back
    to splitting ``instruction`` via LLM."""
    if text_items:
        return [
            {"index": idx + 1, "text": str(t).strip()}
            for idx, t in enumerate(text_items)
            if str(t).strip()
        ]

    # Try lightweight regex split first (CN/EN punctuation + numbered lists).
    # If the result is too short (< 2 items) we fall back to LLM splitting.
    candidates = _heuristic_split(instruction)
    if len(candidates) >= 2:
        return [
            {"index": idx + 1, "text": c}
            for idx, c in enumerate(candidates)
        ]

    # LLM-driven splitting
    sys_prompt = (
        "你是一个任务拆分助手。用户提供的需求里隐含了对多个对象执行同一任务的意图。"
        "请把对象提取出来，**严格输出 JSON 数组**（每个元素是字符串，代表一个对象）。"
        "不要输出解释、不要加 markdown 代码块。"
        "示例：用户说'请帮我分析阿里、腾讯、字节'，你输出 [\"阿里\",\"腾讯\",\"字节\"]。"
    )
    try:
        raw = await _call_llm(instruction, system=sys_prompt, max_tokens=400)
        items = _extract_json_array(raw)
        return [
            {"index": idx + 1, "text": str(it).strip()}
            for idx, it in enumerate(items)
            if str(it).strip()
        ]
    except Exception as exc:
        logger.warning("[internal_batch] LLM split failed: %s", exc)
        return []


_SPLIT_DELIMS_RE = re.compile(r"[、,，;；\n]+")


def _heuristic_split(instruction: str) -> List[str]:
    """Best-effort split when the user clearly enumerated subjects."""
    if not instruction:
        return []
    # Look for the segment after a colon — often "分析：A、B、C"
    if "：" in instruction:
        tail = instruction.rsplit("：", 1)[-1]
    elif ":" in instruction:
        tail = instruction.rsplit(":", 1)[-1]
    else:
        tail = instruction
    parts = [p.strip() for p in _SPLIT_DELIMS_RE.split(tail) if p.strip()]
    parts = [p for p in parts if 1 <= len(p) <= 50]
    return parts


def _extract_json_array(text: str) -> List[Any]:
    """Parse first JSON array found in *text*, tolerating leading/trailing chars."""
    text = (text or "").strip()
    if text.startswith("```"):
        # Strip markdown fence
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("no JSON array in response")
    return json.loads(text[start : end + 1])


def _resolve_xlsx_items(
    file_id: str, db: Session
) -> tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Parse an xlsx artifact → (items, column_names, warnings).

    Hard caps (in core.content.file_parser.parse_xlsx_structured):
      - rows: BATCH_MAX_XLSX_ROWS env (default 2000)
      - 40 columns max
      - cell chars: BATCH_MAX_XLSX_CELL_CHARS env (default 2000)

    Each row becomes a separate downstream LLM call, so the row cap
    bounds plan size and total runtime, not per-call context.

    Each violated cap produces a human-readable warning that the planner
    surfaces in the plan response so the UI can show "已截断 N 条" hints.
    """
    from core.content.file_parser import parse_xlsx_structured

    artifact = db.query(Artifact).filter(Artifact.artifact_id == file_id).first()
    if not artifact:
        raise ValueError(f"file_id {file_id} not found")

    from core.storage import get_storage

    storage = get_storage()
    file_bytes = storage.download_bytes(artifact.storage_key)
    columns, rows, meta = parse_xlsx_structured(file_bytes)

    items = [{"row": idx + 1, **row} for idx, row in enumerate(rows)]

    warnings: List[str] = []
    if meta.get("truncated_rows"):
        warnings.append(
            f"Excel 共 {meta['total_rows']} 行，已截取前 {meta['kept_rows']} 行"
            f"（其余 {meta['truncated_rows']} 行未包含；如需调高，"
            f"可在后端设置 BATCH_MAX_XLSX_ROWS）"
        )
    if meta.get("truncated_columns"):
        warnings.append(
            f"Excel 共 {meta['total_columns']} 列，已截取前 {meta['kept_columns']} 列"
        )
    if meta.get("truncated_cells"):
        warnings.append(
            f"{meta['truncated_cells']} 个超长单元格已截断到 "
            f"{meta.get('max_cell_chars', 2000)} 字以内"
        )

    return items, columns, warnings


_WORD_MAX_CONTENT_CHARS = 8000   # per-file cap (was 20000 — too aggressive)
_WORD_MAX_FILES = 30             # how many files we'll batch in one plan


def _resolve_word_items(
    file_ids: List[str], db: Session
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Parse multiple word/pdf files; one item per file. Returns (items, warnings)."""
    from core.content.file_parser import parse_docx, parse_pdf

    from core.storage import get_storage

    storage = get_storage()
    items: List[Dict[str, Any]] = []
    warnings: List[str] = []

    over_limit = max(0, len(file_ids) - _WORD_MAX_FILES)
    if over_limit:
        warnings.append(
            f"上传了 {len(file_ids)} 份文件，超过单批处理上限"
            f"（{_WORD_MAX_FILES} 份），仅处理前 {_WORD_MAX_FILES} 份"
        )
    file_ids = file_ids[:_WORD_MAX_FILES]

    truncated_files = 0
    for fid in file_ids:
        artifact = db.query(Artifact).filter(Artifact.artifact_id == fid).first()
        if not artifact:
            continue
        try:
            file_bytes = storage.download_bytes(artifact.storage_key)
            name = artifact.filename or fid
            lower = name.lower()
            if lower.endswith(".docx"):
                content = parse_docx(file_bytes)
            elif lower.endswith(".pdf"):
                content = parse_pdf(file_bytes, name)
            else:
                content = file_bytes.decode("utf-8", errors="ignore")
            full_len = len(content)
            if full_len > _WORD_MAX_CONTENT_CHARS:
                content = content[:_WORD_MAX_CONTENT_CHARS] + "…(已截断)"
                truncated_files += 1
            items.append({
                "file_id": fid,
                "file_name": name,
                "content": content,
                "_full_length": full_len,
            })
        except Exception as exc:
            logger.warning("[internal_batch] parse word file %s failed: %s", fid, exc)
            items.append({
                "file_id": fid,
                "file_name": artifact.filename or fid,
                "content": "",
                "_parse_error": str(exc)[:200],
            })
    if truncated_files:
        warnings.append(
            f"{truncated_files} 份文档正文超过 {_WORD_MAX_CONTENT_CHARS} 字，已截断"
        )
    return items, warnings


# ---------------------------------------------------------------------------
# Source-type detection + template inference
# ---------------------------------------------------------------------------


def _detect_source_type(file_ids: List[str], text_items: List[str], db: Session) -> str:
    """Decide which path to take based on inputs."""
    if text_items:
        return "text_list"
    if not file_ids:
        return "text_list"  # fallback: try LLM-split on instruction
    # Inspect first file's mime / extension to decide xlsx vs word
    artifact = db.query(Artifact).filter(Artifact.artifact_id == file_ids[0]).first()
    if artifact and artifact.filename:
        name = artifact.filename.lower()
        if name.endswith((".xlsx", ".xls", ".csv")):
            return "xlsx"
    return "word_files"


def _build_fallback_template(
    instruction: str, source_type: str, placeholder_keys: List[str]
) -> str:
    """Deterministic fallback template — used when LLM inference fails or
    returns junk."""
    inst = (instruction or "对每一项执行任务").strip()
    if source_type == "text_list":
        return f"{inst}\n\n目标对象：{{text}}"
    if source_type == "xlsx":
        # Pick up to 5 most useful columns (skip 'row')
        keys = [k for k in placeholder_keys if k != "row"][:5]
        if not keys:
            return f"{inst}\n\n本行数据：{{row}}"
        placeholder_str = "\n".join(f"- {k}: {{{k}}}" for k in keys)
        return f"{inst}\n\n本行数据：\n{placeholder_str}"
    if source_type == "word_files":
        return f"{inst}\n\n文件：{{file_name}}\n正文：\n{{content}}"
    return f"{inst}\n\n输入：{{text}}"


def _looks_like_valid_template(text: str, placeholder_keys: List[str]) -> bool:
    """A valid template must:
       - be at least 8 chars long after trim
       - reference at least one of the available placeholders, OR
       - if no keys, be at least 20 chars (i.e. instruction-style)
    """
    if not text or len(text.strip()) < 8:
        return False
    if placeholder_keys:
        return any(f"{{{k}}}" in text for k in placeholder_keys)
    return len(text.strip()) >= 20


async def _infer_template(
    instruction: str, source_type: str, placeholder_keys: List[str], preview: List[Dict[str, Any]]
) -> str:
    """Use the LLM to draft a default prompt template using available placeholders.

    Robustness:
      - strips ``<think>`` blocks via _call_llm
      - validates the output references a real placeholder
      - falls back to a deterministic template on any kind of failure
    """
    fallback = _build_fallback_template(instruction, source_type, placeholder_keys)
    if not instruction:
        return fallback

    keys_hint = ", ".join(f"{{{k}}}" for k in placeholder_keys) or "{text}"

    # Trim sample to keep the LLM prompt for template inference small —
    # for word_files items.content can be 8KB each; we don't want to send
    # 16KB into a one-shot call that just needs to *learn the shape*.
    def _shrink(v: Any) -> Any:
        if isinstance(v, str) and len(v) > 200:
            return v[:200] + "…"
        return v

    sample_compact = [
        {k: _shrink(val) for k, val in row.items()}
        for row in preview[:2]
    ]
    sample = json.dumps(sample_compact, ensure_ascii=False)

    sys = (
        "你是一个 prompt 模板设计助手。用户希望对一组对象批量执行同一任务，"
        "请基于其需求与示例数据，生成一段**单条任务**的 prompt 模板（中文）。"
        "模板**必须只使用提供的占位符**（用花括号标注），不要引入新字段。"
        "不要输出解释、不要 markdown 代码块，直接给出模板文本。"
        "模板长度约 1-3 句话即可。"
    )
    user = (
        f"批量目标：{instruction}\n"
        f"数据源类型：{source_type}\n"
        f"可用占位符：{keys_hint}\n"
        f"示例数据：{sample}\n"
        "请直接给出一段中文 prompt 模板，至少包含一个占位符。"
    )
    try:
        raw = await _call_llm(user, system=sys, max_tokens=2000)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```$", "", cleaned).strip()
        # Defensive: drop any lingering <think> opening tag whose closing was
        # cut off by max_tokens (strip_thinking only handles paired tags)
        if "<think>" in cleaned and "</think>" not in cleaned:
            logger.warning("[internal_batch] template had unclosed <think>; using fallback")
            return fallback
        if not _looks_like_valid_template(cleaned, placeholder_keys):
            logger.warning(
                "[internal_batch] inferred template invalid (len=%d, raw=%r); using fallback",
                len(cleaned), cleaned[:80],
            )
            return fallback
        return cleaned
    except Exception as exc:
        logger.warning("[internal_batch] template inference failed: %s", exc)
        return fallback


# ---------------------------------------------------------------------------
# Main resolve endpoint
# ---------------------------------------------------------------------------


@router.post("/resolve", summary="解析批量计划（内部接口）")
async def resolve(
    body: ResolveBody,
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
):
    """供 batch_runner MCP 服务回调的内部接口：解析文件/文本枚举为批量项、推断
    默认 prompt 模板并落库 BatchPlan，返回计划摘要。需 X-Internal-Token 校验。
    """
    _check_internal_token(x_internal_token)

    db: Session = SessionLocal()
    try:
        source_type = _detect_source_type(body.file_ids, body.text_items, db)
        items: List[Dict[str, Any]] = []
        placeholder_keys: List[str] = []
        warnings: List[str] = []

        if source_type == "text_list":
            items = await _resolve_text_items(body.instruction, body.text_items)
            placeholder_keys = ["text", "index"]
            if len(items) > 100:
                warnings.append(
                    f"共解析出 {len(items)} 个对象，仅处理前 100 个（避免运行时间过长）"
                )
                items = items[:100]
        elif source_type == "xlsx":
            if not body.file_ids:
                raise HTTPException(status_code=400, detail="xlsx 解析需要至少一个 file_id")
            items, columns, xlsx_warnings = _resolve_xlsx_items(body.file_ids[0], db)
            placeholder_keys = ["row"] + columns
            warnings.extend(xlsx_warnings)
        elif source_type == "word_files":
            items, word_warnings = _resolve_word_items(body.file_ids, db)
            placeholder_keys = ["file_id", "file_name", "content"]
            warnings.extend(word_warnings)
        else:
            raise HTTPException(status_code=400, detail=f"unknown source_type {source_type}")

        if not items:
            raise HTTPException(status_code=400,
                                detail="无法从输入解析出任何批量项；请检查文件或重新陈述需求。")

        # Owner: prefer explicit user_id, else fall back from chat_id, else 'anonymous'
        owner_user_id = body.user_id or _resolve_user_from_chat(db, body.chat_id) or "anonymous"

        # Default template via LLM
        default_template = await _infer_template(
            body.instruction, source_type, placeholder_keys, items
        )

        plan_id = f"bp_{uuid.uuid4().hex[:16]}"
        plan = BatchPlan(
            plan_id=plan_id,
            user_id=owner_user_id,
            chat_id=body.chat_id or None,
            source_type=source_type,
            items=items,
            placeholder_keys=placeholder_keys,
            instruction=body.instruction,
            prompt_template=default_template,
            max_retries=2,
            status="pending",
            progress={"done": 0, "success": 0, "failed": 0},
            expires_at=datetime.utcnow() + timedelta(hours=24),
        )
        db.add(plan)
        db.commit()

        # Build a compact preview: trim long string values so the LLM /
        # frontend modal don't render multi-KB cells.
        def _shrink_preview(v: Any) -> Any:
            if isinstance(v, str) and len(v) > 120:
                return v[:120] + "…"
            return v

        preview = [
            {k: _shrink_preview(val) for k, val in row.items()}
            for row in items[:3]
        ]

        return {
            "plan_id": plan_id,
            "total": len(items),
            "preview": preview,
            "source_type": source_type,
            "default_template": default_template,
            "placeholder_keys": placeholder_keys,
            "status": "pending",
            "warnings": warnings,
        }
    finally:
        db.close()


def _resolve_user_from_chat(db: Session, chat_id: str) -> Optional[str]:
    if not chat_id:
        return None
    try:
        from core.db.models import ChatSession
        row = db.query(ChatSession.user_id).filter(ChatSession.chat_id == chat_id).first()
        return row.user_id if row else None
    except Exception:
        return None
