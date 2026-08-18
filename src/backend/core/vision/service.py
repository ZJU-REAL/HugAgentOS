"""视觉桥服务 —— 纯文本主模型的「眼睛」。

职责：解析 vision 角色配置 → 校验图片 → 调 provider → 校验结构 → 缓存 → 返回证据。
provider 的协议差异在 :mod:`core.vision.provider`，注入用的文本渲染在
:mod:`core.vision.render`。

三个不能省的工程点：

- **缓存**。同一张图在一轮多步对话里会被反复回看（注入一次、agent 再 view_image 一次、
  下一轮上下文里还在）。key 取 ``sha256(bytes) + focus``，落 Redis，重复识别是纯烧钱
  加纯延迟。参考实现 modlens 是无状态 CLI，没有这一层，我们必须有。
- **并发**。多图上传时并行识别，用信号量兜住并发上限，别把网关打爆。
- **失败即降级**。识别失败返回 ``None``，调用方回落到「模型看不见图」的既有提示，
  绝不因为视觉桥挂了就让整轮对话失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from core.services.model_config import ModelConfigService, ResolvedModelConfig
from core.vision.prompt import build_vision_prompt
from core.vision.provider import VisionProvider
from core.vision.schema import VisionEvidence, parse_evidence

logger = logging.getLogger(__name__)

VISION_ROLE_KEY = "vision"

# 图片字节上限。超限直接拒绝，不发给网关——base64 后体积再涨 1/3，
# 大图既撑爆请求体又拖垮首字延迟。
MAX_IMAGE_BYTES = int(os.getenv("VISION_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
# 证据缓存有效期（秒）
CACHE_TTL_SECONDS = int(os.getenv("VISION_CACHE_TTL_SECONDS", str(7 * 24 * 3600)))
# 同时在途的识别请求数上限
MAX_CONCURRENCY = int(os.getenv("VISION_MAX_CONCURRENCY", "4"))
# 单次识别超时（秒）
CALL_TIMEOUT_SECONDS = float(os.getenv("VISION_CALL_TIMEOUT_SECONDS", "90"))

_CACHE_PREFIX = "vision:ev:v2:"

# 图片格式白名单 + 文件头魔数。扩展名和 MIME 都可以由客户端伪造，字节头不能。
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_mime(image_bytes: bytes, declared: str = "") -> Optional[str]:
    """按文件头判断真实图片类型；不是已知图片格式返回 ``None``。"""
    for magic, mime in _MAGIC:
        if image_bytes.startswith(magic):
            return mime
    # WEBP：RIFF....WEBP
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    # 部分厂商只认 image/jpeg 这类主流类型；declared 仅在魔数无法判定时用作提示，
    # 且必须自称是图片，避免把任意二进制当图片发出去。
    declared = (declared or "").lower().strip()
    if declared.startswith("image/") and declared != "image/svg+xml":
        return None
    return None


@dataclass
class VisionResult:
    """一次识别的完整产出（证据 + 可观测元数据）。"""

    evidence: VisionEvidence
    model: str = ""
    provider: str = ""
    mode: str = ""
    cached: bool = False
    duration_seconds: float = 0.0
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_meta(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "mode": self.mode,
            "cached": self.cached,
            "duration_seconds": round(self.duration_seconds, 2),
            "attempts": self.attempts,
        }


def model_supports_vision(cfg: Optional[ResolvedModelConfig]) -> bool:
    """该模型是否原生多模态。

    以 provider 的 ``extra_config.supports_vision`` 显式开关为准。旧路径靠匹配网关
    报错文案事后判断（``chat_models._is_multimodal_unsupported_error``），换个网关
    文案就失效、还得先发一次注定失败的请求；那条现在只当兜底。
    """
    if cfg is None:
        return False
    return bool((cfg.extra or {}).get("supports_vision"))


def resolve_vision_config() -> Optional[ResolvedModelConfig]:
    """解析视觉桥要用的模型配置。

    优先 ``vision`` 角色；没配就看主模型自己是不是多模态——是的话用它兜底，
    这样「主模型能看图」的部署不用重复配一遍。
    """
    service = ModelConfigService.get_instance()
    cfg = service.resolve(VISION_ROLE_KEY)
    if cfg is not None:
        return cfg
    main = service.resolve("main_agent")
    return main if model_supports_vision(main) else None


def is_available() -> bool:
    """视觉桥当前是否可用（有可调用的多模态模型）。"""
    return resolve_vision_config() is not None


class VisionBridge:
    """图片 → 结构化文字证据。进程内单例。"""

    _instance: Optional["VisionBridge"] = None

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    @classmethod
    def get_instance(cls) -> "VisionBridge":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 对外入口 ──────────────────────────────────────────────────────────

    async def describe(
        self,
        image_bytes: bytes,
        mime_type: str = "",
        *,
        focus: Optional[str] = None,
        use_cache: bool = True,
    ) -> Optional[VisionResult]:
        """识别一张图，返回结构化证据；任何失败都返回 ``None`` 由调用方降级。"""
        if not image_bytes:
            return None
        if len(image_bytes) > MAX_IMAGE_BYTES:
            logger.warning(
                "[vision] image rejected: %d bytes exceeds limit %d",
                len(image_bytes),
                MAX_IMAGE_BYTES,
            )
            return None
        real_mime = sniff_mime(image_bytes, mime_type)
        if real_mime is None:
            logger.warning("[vision] image rejected: unrecognized format (declared=%s)", mime_type)
            return None

        cfg = resolve_vision_config()
        if cfg is None:
            return None

        digest = hashlib.sha256(image_bytes).hexdigest()
        cache_key = self._cache_key(digest, focus, cfg)
        if use_cache:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                return cached

        async with self._semaphore:
            result = await self._describe_uncached(image_bytes, real_mime, focus, cfg)
        if result is None:
            return None
        if use_cache:
            await self._cache_set(cache_key, result)
        return result

    async def describe_many(
        self,
        images: list[tuple[bytes, str]],
        *,
        focus: Optional[str] = None,
    ) -> list[Optional[VisionResult]]:
        """并发识别多张图，顺序与输入一致。"""
        if not images:
            return []
        tasks = [self.describe(data, mime, focus=focus) for data, mime in images]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    # ── 内部 ──────────────────────────────────────────────────────────────

    async def _describe_uncached(
        self,
        image_bytes: bytes,
        mime_type: str,
        focus: Optional[str],
        cfg: ResolvedModelConfig,
    ) -> Optional[VisionResult]:
        started = time.monotonic()
        provider = VisionProvider(cfg)
        prompt = build_vision_prompt(image_kind="inline", focus=focus)
        attempts: list[dict[str, Any]] = []

        call = await provider.describe(
            image_bytes, mime_type, prompt, timeout=CALL_TIMEOUT_SECONDS
        )
        attempts.append(
            {"mode": call.mode or "n/a", "ok": call.ok, "error": call.error or None}
        )
        if not call.ok:
            logger.warning("[vision] all modes failed: %s", call.error)
            return None

        evidence = parse_evidence(call.text)
        if evidence is None or evidence.is_empty():
            # 结构坏了就再要一次纯文本 + 填模板，而不是把坏 JSON 交给主模型
            logger.info("[vision] unusable structure from mode=%s, retrying as plain", call.mode)
            retry = await provider.describe(
                image_bytes, mime_type, prompt, timeout=CALL_TIMEOUT_SECONDS, modes=("plain",)
            )
            attempts.append(
                {"mode": "plain-retry", "ok": retry.ok, "error": retry.error or None}
            )
            evidence = parse_evidence(retry.text) if retry.ok else None
            if evidence is None or evidence.is_empty():
                logger.warning("[vision] retry still unusable; giving up")
                return None
            call = retry

        return VisionResult(
            evidence=evidence,
            model=cfg.model_name,
            provider=cfg.provider,
            mode=call.mode,
            duration_seconds=time.monotonic() - started,
            attempts=attempts,
        )

    @staticmethod
    def _cache_key(digest: str, focus: Optional[str], cfg: ResolvedModelConfig) -> str:
        # model 进 key：换模型后旧证据必须失效，否则会拿着弱模型的结果不放
        focus_digest = hashlib.sha256((focus or "").encode("utf-8")).hexdigest()[:12]
        model_digest = hashlib.sha256(
            f"{cfg.provider}|{cfg.model_name}".encode("utf-8")
        ).hexdigest()[:12]
        return f"{_CACHE_PREFIX}{digest}:{model_digest}:{focus_digest}"

    @staticmethod
    async def _cache_get(key: str) -> Optional[VisionResult]:
        try:
            from core.infra.redis import get_redis

            raw = await get_redis().get(key)
            if not raw:
                return None
            payload = json.loads(raw)
            return VisionResult(
                evidence=VisionEvidence.model_validate(payload["evidence"]),
                model=payload.get("model", ""),
                provider=payload.get("provider", ""),
                mode=payload.get("mode", ""),
                cached=True,
                duration_seconds=payload.get("duration_seconds", 0.0),
                attempts=payload.get("attempts") or [],
            )
        except Exception as exc:  # noqa: BLE001 — 缓存永远不能让主流程失败
            logger.debug("[vision] cache read skipped: %s", exc)
            return None

    @staticmethod
    async def _cache_set(key: str, result: VisionResult) -> None:
        try:
            from core.infra.redis import get_redis

            payload = json.dumps(
                {
                    "evidence": result.evidence.model_dump(exclude_none=True),
                    "model": result.model,
                    "provider": result.provider,
                    "mode": result.mode,
                    "duration_seconds": result.duration_seconds,
                    "attempts": result.attempts,
                },
                ensure_ascii=False,
            )
            await get_redis().set(key, payload, ex=CACHE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vision] cache write skipped: %s", exc)


def get_vision_bridge() -> VisionBridge:
    return VisionBridge.get_instance()
