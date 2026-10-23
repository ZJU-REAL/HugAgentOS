"""Bounded, ephemeral dictation over the OpenAI-compatible audio wire."""
from __future__ import annotations

import io
import time
import wave
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from core.models.repository import get_active_role_provider

MAX_SECONDS = 120
MAX_BYTES = 16000 * 2 * MAX_SECONDS + 4096


@dataclass(frozen=True)
class AudioModel:
    base_url: str
    api_key: str
    model_name: str


def resolve_audio_model(db) -> AudioModel | None:
    provider = get_active_role_provider(db, "audio_transcription", provider_type="audio")
    if not provider or not provider.base_url.strip() or not provider.model_name.strip():
        return None
    if provider.provider not in ("audio_compatible", "openai_compatible", "openai"):
        return None
    model = AudioModel(provider.base_url.strip().rstrip("/"), provider.api_key, provider.model_name)
    try:
        transcription_url(model.base_url)
    except HTTPException:
        return None
    return model


def validate_wav(data: bytes) -> float:
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(413, "录音文件过大，最长支持 120 秒")
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            frames = audio.getnframes()
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError("format")
            duration = frames / 16000
            if duration < 0.15 or duration > MAX_SECONDS:
                raise ValueError("duration")
            if len(audio.readframes(frames)) != frames * 2:
                raise ValueError("truncated")
            return duration
    except (wave.Error, EOFError, ValueError, RuntimeError):
        raise HTTPException(422, "需要 16kHz 单声道 PCM WAV 录音，时长为 0.15 至 120 秒") from None


def transcription_url(base_url: str) -> str:
    try:
        parts = urlsplit(base_url)
        # Accessing .port validates malformed/out-of-range ports; urlsplit alone does not.
        port = parts.port
    except ValueError:
        raise HTTPException(503, "音频模型地址无效，请管理员检查模型配置") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or port == 0:
        raise HTTPException(503, "音频模型地址无效，请管理员检查模型配置")
    url = base_url.rstrip("/") + "/audio/transcriptions"
    try:
        httpx.URL(url)
    except httpx.InvalidURL:
        raise HTTPException(503, "音频模型地址无效，请管理员检查模型配置") from None
    return url


async def transcribe(model: AudioModel, data: bytes, *, client: httpx.AsyncClient | None = None, allow_empty: bool = False) -> str:
    url = transcription_url(model.base_url)
    headers = {"Accept-Encoding": "identity"}
    if model.api_key:
        headers["Authorization"] = f"Bearer {model.api_key}"
    async def send(active_client):
        try:
            # No redirects: credentials must never follow an upstream redirect.
            async with active_client.stream("POST", url, headers=headers,
                    data={"model": model.model_name, "response_format": "json"},
                    files={"file": ("dictation.wav", data, "audio/wav")},
                    timeout=60, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise HTTPException(502, f"音频模型服务暂不可用（HTTP {response.status_code}）")
                # Refuse compressed bodies before httpx can expand a decompression bomb.
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise HTTPException(502, "音频模型返回了不支持的压缩内容")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 256 * 1024:
                        raise HTTPException(502, "音频模型返回内容过大")
                import json
                payload = json.loads(body)
                text = payload.get("text") if isinstance(payload, dict) else None
                if not isinstance(text, str):
                    raise ValueError("invalid transcription response")
                if not text.strip() and not allow_empty:
                    raise HTTPException(422, "未识别到语音，请重新录音")
                return text.strip()
        except httpx.TimeoutException:
            raise HTTPException(504, "语音转写超时，请重试") from None
        except httpx.HTTPError:
            raise HTTPException(502, "无法连接音频模型服务，请稍后重试") from None
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(502, "音频模型返回格式无效") from None
    if client is not None:
        return await send(client)
    async with httpx.AsyncClient() as active_client:
        return await send(active_client)


async def test_audio_connection(base_url: str, api_key: str, model_name: str) -> dict:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 8000)
    started = time.monotonic()
    try:
        await transcribe(AudioModel(base_url, api_key, model_name), output.getvalue(), allow_empty=True)
        error = None
    except HTTPException as exc:
        error = str(exc.detail)
    return {"success": error is None, "latency_ms": int((time.monotonic() - started) * 1000), "error": error}
