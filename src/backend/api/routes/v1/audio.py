"""Voice dictation: authenticated audio in, ephemeral plain text out."""
import asyncio
from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from core.auth.capabilities import resolve_user_capabilities
from core.auth.backend import UserContext, require_auth
from core.db.engine import get_db
from core.db.threads import run_in_session
from core.infra.responses import success_response
from core.chat.audio_transcription import MAX_BYTES, MAX_SECONDS, resolve_audio_model, transcribe, validate_wav

router = APIRouter(prefix="/v1/audio", tags=["Audio"])
# Per-worker admission, no unbounded queue. Every call enters before its first await.
_active: set[str] = set()


@contextmanager
def transcription_slot(user_key: str):
    if user_key in _active or len(_active) >= 4:
        raise HTTPException(429, "语音转写繁忙，请稍后重试", headers={"Retry-After": "3"})
    _active.add(user_key)
    try:
        yield
    finally:
        _active.discard(user_key)


def _can_use_voice(user: UserContext, db: Session) -> bool:
    # Public visitors act in an owner's resource scope, never inherit their voice grant.
    return not user.public_chat_visitor and bool(resolve_user_capabilities(db, user.user_id).get("can_use_voice"))


@router.get("/status")
def audio_status(user: UserContext = Depends(require_auth()), db: Session = Depends(get_db)):
    allowed = _can_use_voice(user, db)
    return success_response(data={"allowed": allowed, "configured": allowed and resolve_audio_model(db) is not None, "max_seconds": MAX_SECONDS})


async def _until_disconnect(request: Request):
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


@router.post("/transcriptions")
async def audio_transcription(request: Request, user: UserContext = Depends(require_auth())):
    def admit(db: Session):
        if not _can_use_voice(user, db):
            raise HTTPException(403, "未开通语音输入权限，请联系管理员")
        return resolve_audio_model(db)

    model = await run_in_session(admit)
    if model is None:
        raise HTTPException(503, "尚未配置音频转写模型，请管理员在模型管理中配置")
    user_key = f"{user.user_id}:{user.public_chat_visitor or ''}"
    with transcription_slot(user_key):
        if request.headers.get("content-type", "").split(";")[0] != "audio/wav":
            raise HTTPException(415, "需要 audio/wav 录音")
        data = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_BYTES:
                        raise HTTPException(413, "录音文件过大，最长支持 120 秒")
                    data.extend(chunk)
        except ClientDisconnect:
            raise HTTPException(499, "录音上传已取消") from None
        except TimeoutError:
            raise HTTPException(408, "录音上传超时，请重试") from None
        duration = validate_wav(bytes(data))
        if await request.is_disconnected():
            raise HTTPException(499, "录音转写已取消")
        work = asyncio.create_task(transcribe(model, bytes(data)))
        disconnected = asyncio.create_task(_until_disconnect(request))
        try:
            done, _ = await asyncio.wait((work, disconnected), timeout=65, return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                raise HTTPException(499, "录音转写已取消")
            if work not in done:
                raise HTTPException(504, "语音转写超时，请重试")
            return success_response(data={"text": work.result(), "duration_seconds": duration})
        finally:
            for task in (work, disconnected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, disconnected, return_exceptions=True)
