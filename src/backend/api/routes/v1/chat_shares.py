"""Chat share link routes."""

import calendar
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from core.auth.backend import UserContext, get_current_user
from core.config.settings import settings
from core.infra.redis import get_redis
from core.infra.logging import get_logger
from core.infra.responses import created_response, success_response

router = APIRouter(prefix="/v1/chat-shares", tags=["Chat Shares"])
logger = get_logger(__name__)

SHARE_EXPIRY_OPTION = Literal["3d", "15d", "3m", "permanent"]
SHARE_DURATION_SECONDS = {
    "3d": 3 * 24 * 60 * 60,
    "15d": 15 * 24 * 60 * 60,
}
SHARE_REDIS_PREFIX = "chat_share:"
SHARE_META_REDIS_PREFIX = "chat_share_meta:"
SHARE_HISTORY_REDIS_PREFIX = "chat_share_history:"
_MEMORY_SHARE_PAYLOADS: dict[str, dict] = {}
_MEMORY_SHARE_METADATA: dict[str, dict] = {}
_MEMORY_SHARE_HISTORY: dict[str, list[tuple[float, str]]] = {}
_REDIS_FALLBACK_EXCEPTIONS = (RedisError, OSError, TimeoutError)


class ShareMessageItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field("", min_length=0)
    is_markdown: bool = False
    created_at: Optional[str] = None
    plan_data: Optional[Any] = None  # structured plan card data (preview / executing / complete)


class CreateChatShareRequest(BaseModel):
    chat_id: str = Field(..., min_length=1)
    title: str = Field("分享会话", min_length=1, max_length=500)
    items: List[ShareMessageItem] = Field(..., min_length=1)
    expiry_option: SHARE_EXPIRY_OPTION = "15d"
    origin_message_ts: Optional[int] = None


def _share_key(share_id: str) -> str:
    return f"{SHARE_REDIS_PREFIX}{share_id}"


def _share_meta_key(share_id: str) -> str:
    return f"{SHARE_META_REDIS_PREFIX}{share_id}"


def _share_history_key(user_id: str) -> str:
    return f"{SHARE_HISTORY_REDIS_PREFIX}{user_id}"


class ShareRecordItem(BaseModel):
    share_id: str
    chat_id: str
    origin_message_ts: Optional[int] = None
    title: str
    preview_url: str
    created_at: str
    expires_at: Optional[str] = None
    expiry_option: Optional[SHARE_EXPIRY_OPTION] = None
    created_by: str
    created_by_username: Optional[str] = None
    status: Literal["valid", "expired"]
    view_count: int = 0
    revoked: bool = False


def _resolve_share_status(payload: dict, now: datetime) -> Literal["valid", "expired"]:
    if payload.get("revoked"):
        return "expired"

    expires_at_raw = payload.get("expires_at")
    if not expires_at_raw:
        return "valid"
    try:
        expires_at = datetime.fromisoformat(expires_at_raw) if isinstance(expires_at_raw, str) else None
    except ValueError:
        expires_at = None

    if expires_at and expires_at > now:
        return "valid"
    return "expired"


def _add_months(base: datetime, months: int) -> datetime:
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)


def _resolve_expiry(option: SHARE_EXPIRY_OPTION, now: datetime) -> tuple[Optional[datetime], Optional[int]]:
    if option == "permanent":
        return None, None
    if option == "3m":
        expires_at = _add_months(now, 3)
        return expires_at, max(1, int((expires_at - now).total_seconds()))
    ttl_seconds = SHARE_DURATION_SECONDS[option]
    return now + timedelta(seconds=ttl_seconds), ttl_seconds


def _use_memory_share_store() -> bool:
    return settings.session.store_type == "memory"


def _prune_memory_shares(now: datetime) -> None:
    expired_share_ids = [
        share_id
        for share_id, payload in _MEMORY_SHARE_PAYLOADS.items()
        if _resolve_share_status(payload, now) == "expired"
    ]
    for share_id in expired_share_ids:
        _MEMORY_SHARE_PAYLOADS.pop(share_id, None)
        _MEMORY_SHARE_METADATA.pop(share_id, None)

    if not expired_share_ids:
        return

    expired_set = set(expired_share_ids)
    for user_id, history in list(_MEMORY_SHARE_HISTORY.items()):
        next_history = [(score, share_id) for score, share_id in history if share_id not in expired_set]
        if next_history:
            _MEMORY_SHARE_HISTORY[user_id] = next_history
        else:
            _MEMORY_SHARE_HISTORY.pop(user_id, None)


async def _set_share_payload(share_id: str, payload: dict, ttl_seconds: Optional[int]) -> None:
    if _use_memory_share_store():
        _prune_memory_shares(datetime.now(timezone.utc))
        _MEMORY_SHARE_PAYLOADS[share_id] = payload
        return

    redis = get_redis()
    try:
        if ttl_seconds is None:
            await redis.set(_share_key(share_id), json.dumps(payload, ensure_ascii=False))
        else:
            await redis.set(_share_key(share_id), json.dumps(payload, ensure_ascii=False), ex=ttl_seconds)
        return
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_store_fallback_to_memory", share_id=share_id, error=str(exc))

    _prune_memory_shares(datetime.now(timezone.utc))
    _MEMORY_SHARE_PAYLOADS[share_id] = payload


async def _set_share_metadata(share_id: str, metadata: dict) -> None:
    if _use_memory_share_store():
        _prune_memory_shares(datetime.now(timezone.utc))
        _MEMORY_SHARE_METADATA[share_id] = metadata
        return

    redis = get_redis()
    try:
        await redis.set(_share_meta_key(share_id), json.dumps(metadata, ensure_ascii=False))
        return
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_meta_fallback_to_memory", share_id=share_id, error=str(exc))

    _prune_memory_shares(datetime.now(timezone.utc))
    _MEMORY_SHARE_METADATA[share_id] = metadata


async def _append_share_history(user_id: str, share_id: str, score: float) -> None:
    if _use_memory_share_store():
        _prune_memory_shares(datetime.now(timezone.utc))
        history = _MEMORY_SHARE_HISTORY.setdefault(user_id, [])
        history = [
            (existing_score, existing_share_id)
            for existing_score, existing_share_id in history
            if existing_share_id != share_id
        ]
        history.append((score, share_id))
        history.sort(key=lambda item: item[0], reverse=True)
        _MEMORY_SHARE_HISTORY[user_id] = history
        return

    redis = get_redis()
    try:
        await redis.zadd(_share_history_key(user_id), {share_id: score})
        return
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_history_fallback_to_memory", user_id=user_id, share_id=share_id, error=str(exc))

    _prune_memory_shares(datetime.now(timezone.utc))
    history = _MEMORY_SHARE_HISTORY.setdefault(user_id, [])
    history = [(existing_score, existing_share_id) for existing_score, existing_share_id in history if existing_share_id != share_id]
    history.append((score, share_id))
    history.sort(key=lambda item: item[0], reverse=True)
    _MEMORY_SHARE_HISTORY[user_id] = history


async def _get_share_payload(share_id: str) -> Optional[str]:
    if _use_memory_share_store():
        _prune_memory_shares(datetime.now(timezone.utc))
        payload = _MEMORY_SHARE_PAYLOADS.get(share_id)
        return json.dumps(payload, ensure_ascii=False) if payload else None

    redis = get_redis()
    try:
        return await redis.get(_share_key(share_id))
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_payload_read_fallback_to_memory", share_id=share_id, error=str(exc))

    _prune_memory_shares(datetime.now(timezone.utc))
    payload = _MEMORY_SHARE_PAYLOADS.get(share_id)
    return json.dumps(payload, ensure_ascii=False) if payload else None


async def _get_share_metadata(share_id: str) -> Optional[str]:
    if _use_memory_share_store():
        _prune_memory_shares(datetime.now(timezone.utc))
        metadata = _MEMORY_SHARE_METADATA.get(share_id)
        return json.dumps(metadata, ensure_ascii=False) if metadata else None

    redis = get_redis()
    try:
        return await redis.get(_share_meta_key(share_id))
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_meta_read_fallback_to_memory", share_id=share_id, error=str(exc))

    _prune_memory_shares(datetime.now(timezone.utc))
    metadata = _MEMORY_SHARE_METADATA.get(share_id)
    return json.dumps(metadata, ensure_ascii=False) if metadata else None


async def _delete_share_payload(share_id: str) -> None:
    if _use_memory_share_store():
        _MEMORY_SHARE_PAYLOADS.pop(share_id, None)
        return

    redis = get_redis()
    try:
        await redis.delete(_share_key(share_id))
        return
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_payload_delete_fallback_to_memory", share_id=share_id, error=str(exc))

    _MEMORY_SHARE_PAYLOADS.pop(share_id, None)


async def _delete_share_metadata(share_id: str) -> None:
    if _use_memory_share_store():
        _MEMORY_SHARE_METADATA.pop(share_id, None)
        return

    redis = get_redis()
    try:
        await redis.delete(_share_meta_key(share_id))
        return
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_meta_delete_fallback_to_memory", share_id=share_id, error=str(exc))

    _MEMORY_SHARE_METADATA.pop(share_id, None)


def _drop_from_memory_history(user_id: str, share_id: str) -> None:
    history = _MEMORY_SHARE_HISTORY.get(user_id)
    if not history:
        return
    next_history = [(score, existing_share_id) for score, existing_share_id in history if existing_share_id != share_id]
    if next_history:
        _MEMORY_SHARE_HISTORY[user_id] = next_history
    else:
        _MEMORY_SHARE_HISTORY.pop(user_id, None)


async def _remove_share_history(user_id: str, share_id: str) -> None:
    if _use_memory_share_store():
        _drop_from_memory_history(user_id, share_id)
        return

    redis = get_redis()
    try:
        await redis.zrem(_share_history_key(user_id), share_id)
        return
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_history_delete_fallback_to_memory", user_id=user_id, share_id=share_id, error=str(exc))

    _drop_from_memory_history(user_id, share_id)


async def _list_share_ids(user_id: str) -> list[str]:
    if _use_memory_share_store():
        _prune_memory_shares(datetime.now(timezone.utc))
        return [share_id for _, share_id in _MEMORY_SHARE_HISTORY.get(user_id, [])]

    redis = get_redis()
    try:
        share_ids = await redis.zrevrange(_share_history_key(user_id), 0, -1)
        return list(share_ids)
    except _REDIS_FALLBACK_EXCEPTIONS as exc:
        logger.warning("chat_share_history_read_fallback_to_memory", user_id=user_id, error=str(exc))

    _prune_memory_shares(datetime.now(timezone.utc))
    return [share_id for _, share_id in _MEMORY_SHARE_HISTORY.get(user_id, [])]


@router.post("", status_code=status.HTTP_201_CREATED, summary="生成会话分享链接")
async def create_chat_share(
    request: CreateChatShareRequest,
    user: UserContext = Depends(get_current_user),
):
    """生成会话分享链接：把传入的消息快照存入分享库并返回预览 URL。

    按 expiry_option（3d/15d/3m/permanent）设置有效期，记录创建者并写入其分享历史。需登录。
    """
    now = datetime.now(timezone.utc)
    expires_at, ttl_seconds = _resolve_expiry(request.expiry_option, now)
    share_id = secrets.token_urlsafe(18)
    payload = {
        "share_id": share_id,
        "chat_id": request.chat_id,
        "origin_message_ts": request.origin_message_ts,
        "title": request.title,
        "items": [item.model_dump() for item in request.items],
        "created_by": user.user_id,
        "created_by_username": user.username,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "expiry_option": request.expiry_option,
    }

    await _set_share_payload(share_id, payload, ttl_seconds)
    metadata = {
        "share_id": share_id,
        "chat_id": request.chat_id,
        "origin_message_ts": request.origin_message_ts,
        "title": request.title,
        "preview_url": f"/?share={share_id}",
        "created_by": user.user_id,
        "created_by_username": user.username,
        "created_at": payload["created_at"],
        "expires_at": payload["expires_at"],
        "expiry_option": request.expiry_option,
        "view_count": 0,
        "revoked": False,
    }
    await _set_share_metadata(share_id, metadata)
    await _append_share_history(user.user_id, share_id, now.timestamp())

    return created_response(
        data={
            "share_id": share_id,
            "preview_url": metadata["preview_url"],
            "expires_at": payload["expires_at"],
            "expiry_option": request.expiry_option,
        },
        message="Chat share created successfully",
    )


@router.get("", summary="获取当前用户的分享记录")
async def list_chat_shares(
    user: UserContext = Depends(get_current_user),
):
    """列出当前用户创建的全部分享记录，并按当前时间标注 valid / expired 状态。需登录。"""
    share_ids = await _list_share_ids(user.user_id)
    now = datetime.now(timezone.utc)
    records: List[dict] = []

    for share_id in share_ids:
        raw = await _get_share_metadata(share_id)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        payload["view_count"] = max(0, int(payload.get("view_count") or 0))
        payload["revoked"] = bool(payload.get("revoked"))
        payload["status"] = _resolve_share_status(payload, now)
        records.append(ShareRecordItem(**payload).model_dump())

    return success_response(data={"items": records}, message="Chat share records retrieved successfully")


@router.get("/{share_id}", summary="获取会话分享内容")
async def get_chat_share(share_id: str):
    """按 share_id 读取分享的会话内容（公开访问，无需登录）。

    链接失效、已撤销或不存在均返回 404；每次成功读取会把浏览计数加一。
    """
    raw = await _get_share_payload(share_id)
    if not raw:
        raise HTTPException(status_code=404, detail="链接已失效")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="分享内容损坏") from exc

    raw_meta = await _get_share_metadata(share_id)
    metadata = None
    if raw_meta:
        try:
            metadata = json.loads(raw_meta)
        except json.JSONDecodeError:
            metadata = None

    if isinstance(metadata, dict) and metadata.get("revoked"):
        raise HTTPException(status_code=404, detail="链接已失效")

    if isinstance(metadata, dict):
        metadata["view_count"] = max(0, int(metadata.get("view_count") or 0)) + 1
        await _set_share_metadata(share_id, metadata)

    return success_response(data=payload, message="Chat share retrieved successfully")


@router.post("/{share_id}/revoke", summary="终止会话分享链接访问")
async def revoke_chat_share(
    share_id: str,
    user: UserContext = Depends(get_current_user),
):
    """终止分享链接的访问：把链接标记为已撤销，访客访问将返回失效。

    仅链接创建者可操作（非属主 403）；记录保留，可再通过 restore 恢复。需登录。
    """
    raw_meta = await _get_share_metadata(share_id)
    if not raw_meta:
        raise HTTPException(status_code=404, detail="链接不存在或已失效")

    try:
        metadata = json.loads(raw_meta)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="分享记录损坏") from exc

    if metadata.get("created_by") != user.user_id:
        raise HTTPException(status_code=403, detail="无权操作该分享链接")

    metadata["revoked"] = True
    metadata["view_count"] = max(0, int(metadata.get("view_count") or 0))
    await _set_share_metadata(share_id, metadata)

    return success_response(
        data={"share_id": share_id, "status": "expired"},
        message="Chat share revoked successfully",
    )


@router.post("/{share_id}/restore", summary="恢复会话分享链接访问")
async def restore_chat_share(
    share_id: str,
    user: UserContext = Depends(get_current_user),
):
    """恢复已撤销分享链接的访问（撤销操作的逆操作）。

    仅创建者可操作（非属主 403）；若链接已超过有效期则无法恢复，返回 400。需登录。
    """
    raw_meta = await _get_share_metadata(share_id)
    if not raw_meta:
        raise HTTPException(status_code=404, detail="链接不存在或已失效")

    try:
        metadata = json.loads(raw_meta)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="分享记录损坏") from exc

    if metadata.get("created_by") != user.user_id:
        raise HTTPException(status_code=403, detail="无权操作该分享链接")

    expires_at_raw = metadata.get("expires_at")
    if not expires_at_raw:
        expires_at = None
    else:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw) if isinstance(expires_at_raw, str) else None
        except ValueError:
            expires_at = None

    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="链接已超过有效期，无法恢复访问")

    metadata["revoked"] = False
    metadata["view_count"] = max(0, int(metadata.get("view_count") or 0))
    await _set_share_metadata(share_id, metadata)

    return success_response(
        data={"share_id": share_id, "status": "valid"},
        message="Chat share restored successfully",
    )


@router.delete("/{share_id}", summary="删除会话分享记录")
async def delete_chat_share(
    share_id: str,
    user: UserContext = Depends(get_current_user),
):
    """彻底删除分享记录：移除分享内容、元数据及创建者历史，链接永久失效。

    仅创建者可操作（非属主 403）；与撤销不同，删除不可恢复。需登录。
    """
    raw_meta = await _get_share_metadata(share_id)
    if not raw_meta:
        raise HTTPException(status_code=404, detail="分享记录不存在")

    try:
        metadata = json.loads(raw_meta)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="分享记录损坏") from exc

    if metadata.get("created_by") != user.user_id:
        raise HTTPException(status_code=403, detail="无权操作该分享链接")

    await _delete_share_payload(share_id)
    await _delete_share_metadata(share_id)
    await _remove_share_history(user.user_id, share_id)

    return success_response(
        data={"share_id": share_id},
        message="Chat share deleted successfully",
    )
