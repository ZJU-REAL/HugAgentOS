"""视觉引擎 provider —— 把一张图 + 一段指令发给多模态模型，拿回原始文本。

三条协议，按 ``core/llm/providers/registry.py`` 的 provider 归属分派：

- **OpenAI 兼容**（绝大多数厂商，含 DashScope 兼容模式、Ollama 的 ``/v1``）：
  ``chat/completions`` + ``image_url`` 内联 data URI。
- **Anthropic 原生**：``/v1/messages`` + ``source.type=base64``。
- **Gemini 原生**：``generateContent`` + ``inline_data``。

这里只管协议差异，不管重试/缓存/降级——那些在 :mod:`core.vision.service`。

结构化输出按 **三级降级**尝试，逐级放宽：
``json_schema`` → ``json_object`` → 纯文本 + 填模板指令。弱网关对前两级常报 400，
一次降级换一次成功，比直接上最宽松的一级质量更好。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from core.llm.providers.registry import get_spec
from core.services.model_config import ResolvedModelConfig
from core.vision.prompt import JSON_TEMPLATE_INSTRUCTION
from core.vision.schema import VISION_JSON_SCHEMA

logger = logging.getLogger(__name__)

# 结构化输出模式，从严到宽
MODE_JSON_SCHEMA = "json_schema"
MODE_JSON_OBJECT = "json_object"
MODE_PLAIN = "plain"

_MODES = (MODE_JSON_SCHEMA, MODE_JSON_OBJECT, MODE_PLAIN)


@dataclass
class VisionCallResult:
    """一次 provider 调用的结果。"""

    text: str = ""
    mode: str = ""
    error: str = ""
    usage: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def _data_uri(image_bytes: bytes, mime_type: str) -> str:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def _normalize_base_url(cfg: ResolvedModelConfig) -> str:
    """把 base_url 归一到该协议真正的请求根。"""
    url = (cfg.base_url or "").strip().rstrip("/")
    spec = get_spec(cfg.provider)
    # Ollama 的原生根是 :11434，OpenAI 兼容层挂在 /v1 下
    if cfg.provider == "ollama" and url and not url.endswith("/v1"):
        url = f"{url}/v1"
    if not url and spec.base_url_template and "<" not in spec.base_url_template:
        url = spec.base_url_template.rstrip("/")
    return url


class VisionProvider:
    """按配置调用一个多模态模型。"""

    def __init__(self, cfg: ResolvedModelConfig) -> None:
        self.cfg = cfg
        self.spec = get_spec(cfg.provider)
        self.base_url = _normalize_base_url(cfg)

    # ── 对外入口 ──────────────────────────────────────────────────────────

    async def describe(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        *,
        timeout: Optional[float] = None,
        modes: tuple[str, ...] = _MODES,
    ) -> VisionCallResult:
        """把图片和指令发给模型，返回原始文本（不做 JSON 解析）。"""
        errors: list[str] = []
        for mode in modes:
            try:
                result = await self._call(image_bytes, mime_type, prompt, mode, timeout)
            except Exception as exc:  # noqa: BLE001 — 逐级降级，最后一级才把错误抛给上层
                errors.append(f"{mode}: {exc}")
                logger.info("[vision] mode=%s failed: %s", mode, exc)
                continue
            if result.ok:
                return result
            errors.append(f"{mode}: empty response")
        return VisionCallResult(error="; ".join(errors) or "no attempt made")

    # ── 协议分派 ──────────────────────────────────────────────────────────

    async def _call(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        mode: str,
        timeout: Optional[float],
    ) -> VisionCallResult:
        effective_timeout = timeout or float(self.cfg.timeout or 120)
        if self.cfg.provider == "anthropic":
            return await self._call_anthropic(
                image_bytes, mime_type, prompt, mode, effective_timeout
            )
        if self.cfg.provider == "gemini":
            return await self._call_gemini(
                image_bytes, mime_type, prompt, mode, effective_timeout
            )
        return await self._call_openai_compat(
            image_bytes, mime_type, prompt, mode, effective_timeout
        )

    # ── OpenAI 兼容 ───────────────────────────────────────────────────────

    async def _call_openai_compat(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        mode: str,
        timeout: float,
    ) -> VisionCallResult:
        if not self.base_url:
            raise RuntimeError("视觉模型未配置 base_url")
        text_prompt = prompt if mode != MODE_PLAIN else f"{prompt}\n\n{JSON_TEMPLATE_INSTRUCTION}"
        payload: dict[str, Any] = {
            "model": self.cfg.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": _data_uri(image_bytes, mime_type)},
                        },
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": self.cfg.max_tokens or 4096,
            "stream": False,
        }
        if mode == MODE_JSON_SCHEMA:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "vision_evidence",
                    "strict": True,
                    "schema": VISION_JSON_SCHEMA,
                },
            }
        elif mode == MODE_JSON_OBJECT:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = f"{self.base_url}/chat/completions"
        # Azure OpenAI 的路径形态不同：deployment 落在 URL 上，api-version 走 query
        if self.cfg.provider == "azure_openai":
            extra = self.cfg.provider_extra or {}
            deployment = extra.get("deployment") or self.cfg.model_name
            api_version = extra.get("api_version", "")
            url = f"{self.base_url}/openai/deployments/{deployment}/chat/completions"
            if api_version:
                url = f"{url}?api-version={api_version}"
            headers.pop("Authorization", None)
            if self.cfg.api_key:
                headers["api-key"] = self.cfg.api_key

        data = await self._post_json(url, headers, payload, timeout)
        choices = data.get("choices") or []
        content = ""
        if choices:
            message = choices[0].get("message") or {}
            raw = message.get("content")
            if isinstance(raw, list):  # 少数网关把 content 拆成块数组
                content = "".join(
                    b.get("text", "") for b in raw if isinstance(b, dict)
                )
            else:
                content = raw or ""
        return VisionCallResult(text=content, mode=mode, usage=data.get("usage"))

    # ── Anthropic 原生 ────────────────────────────────────────────────────

    async def _call_anthropic(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        mode: str,
        timeout: float,
    ) -> VisionCallResult:
        # Anthropic 没有 response_format；json_schema/json_object 两级对它无意义，
        # 直接只跑一次带填模板指令的请求，避免同一次调用重复三遍。
        if mode != _MODES[0]:
            raise RuntimeError("anthropic: already attempted")
        base = self.base_url or "https://api.anthropic.com"
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.cfg.api_key:
            headers["x-api-key"] = self.cfg.api_key
        payload = {
            "model": self.cfg.model_name,
            "max_tokens": self.cfg.max_tokens or 4096,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": f"{prompt}\n\n{JSON_TEMPLATE_INSTRUCTION}"},
                    ],
                }
            ],
        }
        data = await self._post_json(f"{base}/v1/messages", headers, payload, timeout)
        text = "".join(
            b.get("text", "")
            for b in (data.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return VisionCallResult(text=text, mode=MODE_PLAIN, usage=data.get("usage"))

    # ── Gemini 原生 ───────────────────────────────────────────────────────

    async def _call_gemini(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        mode: str,
        timeout: float,
    ) -> VisionCallResult:
        if mode == MODE_JSON_OBJECT:
            # Gemini 用 responseMimeType/responseSchema，没有 json_object 这一档
            raise RuntimeError("gemini: mode not applicable")
        base = self.base_url or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{self.cfg.model_name}:generateContent"
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["x-goog-api-key"] = self.cfg.api_key
        text_prompt = prompt if mode != MODE_PLAIN else f"{prompt}\n\n{JSON_TEMPLATE_INSTRUCTION}"
        generation_config: dict[str, Any] = {"temperature": 0}
        if mode == MODE_JSON_SCHEMA:
            generation_config["responseMimeType"] = "application/json"
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": text_prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": generation_config,
        }
        data = await self._post_json(url, headers, payload, timeout)
        candidates = data.get("candidates") or []
        text = ""
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return VisionCallResult(text=text, mode=mode, usage=data.get("usageMetadata"))

    # ── HTTP ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _post_json(
        url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            # 截断 body：视觉请求的报错里常回显整段 base64，原样打日志会淹掉一切
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:400]}")
        return response.json()
