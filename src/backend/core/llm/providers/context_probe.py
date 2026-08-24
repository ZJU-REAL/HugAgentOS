"""Automatic discovery of a model's context window (context_length).

Why this exists
---------------
``extra_config.context_length`` is the single source of truth for history trimming and
the auto-compaction trigger (see ``core/llm/context_manager.resolve_model_context_window``),
and it has **no runtime fallback** on purpose: a silent 128k default once made a real
256k model compact at half its window. That correctness choice pushed the cost onto
whoever onboards a model — leave the field empty and the model simply does not run.

This module removes that cost by discovering the window at *configuration* time, in
decreasing order of trust:

1. ``models_endpoint``  — ``GET {base_url}/models``, read the window off the entry whose
   ``id`` matches the model. vLLM / SGLang report ``max_model_len``, OpenRouter reports
   ``context_length``, LiteLLM-style gateways report ``max_input_tokens``. **high** confidence.
2. ``ollama_show``      — Ollama's native ``POST /api/show`` exposes
   ``model_info["<arch>.context_length"]``. **high** confidence.
3. ``max_tokens_probe`` — one deliberately over-sized ``max_tokens`` request. The upstream
   rejects it during request validation (no inference, no tokens billed) and many servers
   name the real limit in the error text. **medium** confidence, opt-in only.
4. ``name_heuristic``   — a model-family table mirroring the frontend's
   ``utils/contextUsage.ts`` ``CONTEXT_WINDOWS``. **low** confidence.

Deliberately NOT implemented: the "send an over-long prompt and read the error" probe.
It is the most reliable signal on vLLM, but gateways that *accept* the request happily
run inference over a six-figure token prompt — a probe that can bill real money must not
be a background behaviour.

Guarding against under-reporting: an upstream that only says
``Range of max_tokens should be [1, N]`` is describing the **output** cap, which on many
vendors is far smaller than the context window (Anthropic: 64k output vs 200k context).
Such numbers are reported in ``detail`` for a human to read but never adopted as the
window — being wrong-low here reintroduces exactly the compact-at-half-window bug.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Sanity bounds (tokens). Values outside are treated as noise rather than a window.
MIN_PLAUSIBLE_WINDOW = 1024
MAX_PLAUSIBLE_WINDOW = 20_000_000

# max_tokens value used by the error probe: large enough that every server rejects it
# during validation, small enough to stay a plain integer.
_ABSURD_MAX_TOKENS = 99_999_999

# Window fields seen on OpenAI-compatible /v1/models entries, most specific first.
_WINDOW_KEYS: tuple[str, ...] = (
    "max_model_len",        # vLLM / SGLang
    "context_length",       # OpenRouter, Together
    "context_window",       # Anthropic-style gateways
    "max_context_length",
    "max_input_tokens",     # LiteLLM model_info
    "max_context_tokens",
    "n_ctx",                # llama.cpp
)

# Nested containers that may hold the fields above (LiteLLM: {"model_info": {...}}).
_NESTED_KEYS: tuple[str, ...] = ("model_info", "limits", "meta", "spec", "config", "top_provider")

# Error-text patterns that unambiguously name the *context* window.
_CONTEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # vLLM: "max_tokens=99999999 cannot be greater than max_model_len=max_total_tokens=131072"
    re.compile(r"max_model_len\s*=\s*(?:max_total_tokens\s*=\s*)?(\d+)", re.I),
    # OpenAI: "This model's maximum context length is 128000 tokens"
    re.compile(r"maximum context length is\s*(\d+)", re.I),
    # vLLM prompt-too-long: "the model's context length is only 131072 tokens"
    re.compile(r"context length is only\s*(\d+)", re.I),
    re.compile(r"context[_ ]window[\"'\s:=]+(\d+)", re.I),
)

# Output-cap patterns: informative for a human, never adopted as the window.
_OUTPUT_CAP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"range of max_tokens should be\s*\[\s*\d+\s*,\s*(\d+)\s*\]", re.I),
    re.compile(r"max_tokens\D{0,40}?(?:less than or equal to|at most|<=)\s*(\d+)", re.I),
)

# Model-family fallback. Mirrors src/frontend/src/utils/contextUsage.ts::CONTEXT_WINDOWS —
# keep the two in step when onboarding a new family.
NAME_WINDOW_RULES: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"claude", re.I), 200_000),
    (re.compile(r"gpt-4\.1|gpt-4o|gpt-4-turbo|o1|o3|o4-mini", re.I), 128_000),
    (re.compile(r"gpt-4-32k", re.I), 32_768),
    (re.compile(r"gpt-4", re.I), 8_192),
    (re.compile(r"gpt-3\.5", re.I), 16_385),
    (re.compile(r"gemini", re.I), 1_000_000),
    (re.compile(r"deepseek", re.I), 64_000),
    (re.compile(r"(qwen|通义).*(max|plus|turbo|long)", re.I), 128_000),
    (re.compile(r"qwen|通义", re.I), 32_768),
    (re.compile(r"kimi|moonshot", re.I), 200_000),
    (re.compile(r"glm-4|chatglm|智谱", re.I), 128_000),
    (re.compile(r"doubao|豆包", re.I), 128_000),
    (re.compile(r"ernie|文心", re.I), 128_000),
    (re.compile(r"yi-", re.I), 200_000),
)

# Human-readable label per source, surfaced in the admin UI.
SOURCE_LABELS: dict[str, str] = {
    "models_endpoint": "供应商模型列表接口",
    "ollama_show": "Ollama /api/show",
    "max_tokens_probe": "上游超限报错回报",
    "name_heuristic": "模型名族推断",
}


@dataclass
class ContextProbeResult:
    """Outcome of one discovery attempt.

    ``context_length`` is 0 when nothing trustworthy was found; ``notes`` carries the
    per-stage explanation (why a stage was skipped, what it saw) for the admin UI.
    """

    context_length: int = 0
    source: str = ""
    confidence: str = "none"  # high | medium | low | none
    detail: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.context_length > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_length": self.context_length,
            "source": self.source,
            "source_label": SOURCE_LABELS.get(self.source, self.source),
            "confidence": self.confidence,
            "detail": self.detail,
            "notes": list(self.notes),
        }


def _sane(value: Any) -> int:
    """Coerce a reported window to a plausible positive token count (0 = reject)."""
    try:
        num = int(float(value))
    except (TypeError, ValueError):
        return 0
    if MIN_PLAUSIBLE_WINDOW <= num <= MAX_PLAUSIBLE_WINDOW:
        return num
    return 0


def _pick_window(entry: Any, _depth: int = 0) -> tuple[int, str]:
    """Find a window field on a /v1/models entry. Returns (tokens, field_path)."""
    if not isinstance(entry, dict) or _depth > 2:
        return 0, ""
    for key in _WINDOW_KEYS:
        if key in entry:
            value = _sane(entry[key])
            if value:
                return value, key
    for nested in _NESTED_KEYS:
        value, path = _pick_window(entry.get(nested), _depth + 1)
        if value:
            return value, f"{nested}.{path}"
    return 0, ""


def _match_entry(items: list[Any], model_name: str) -> Optional[dict]:
    """Pick the /v1/models entry describing ``model_name``.

    Gateways list hundreds of models, so an exact ``id`` match comes first and a
    case-insensitive match is the only fallback. We never fall back to "the first entry"
    on a multi-model listing — that would report an unrelated model's window.
    """
    target = (model_name or "").strip()
    entries = [it for it in items if isinstance(it, dict)]
    for item in entries:
        if str(item.get("id") or "") == target:
            return item
    lowered = target.lower()
    for item in entries:
        if str(item.get("id") or "").lower() == lowered:
            return item
    # A single-model server (self-hosted vLLM) is unambiguous even if the alias differs.
    if len(entries) == 1:
        return entries[0]
    return None


def _auth_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _from_models_endpoint(
    base_url: str, api_key: str, model_name: str, timeout: int
) -> tuple[int, str]:
    """Stage 1: read the window off ``GET {base_url}/models``."""
    url = f"{base_url.rstrip('/')}/models"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=_auth_headers(api_key))
    if resp.status_code != 200:
        return 0, f"模型列表接口返回 HTTP {resp.status_code}"
    payload = resp.json()
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        return 0, "模型列表接口未返回条目"
    entry = _match_entry(items, model_name)
    if entry is None:
        return 0, f"模型列表中未找到 {model_name}（共 {len(items)} 个条目）"
    window, path = _pick_window(entry)
    if not window:
        return 0, "模型列表条目中没有上下文长度字段（网关型供应商通常如此）"
    return window, f"{path}={window}"


async def _from_ollama_show(base_url: str, model_name: str, timeout: int) -> tuple[int, str]:
    """Stage 2: Ollama's native ``POST /api/show`` model_info block."""
    root = base_url.rstrip("/")
    for suffix in ("/v1", "/api"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    url = f"{root}/api/show"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json={"model": model_name})
    if resp.status_code != 200:
        return 0, f"/api/show 返回 HTTP {resp.status_code}"
    info = (resp.json() or {}).get("model_info") or {}
    for key, value in info.items():
        if key.endswith(".context_length"):
            window = _sane(value)
            if window:
                return window, f"model_info.{key}={window}"
    return 0, "/api/show 未回报 context_length"


async def _from_max_tokens_probe(
    base_url: str, api_key: str, model_name: str, timeout: int
) -> tuple[int, str]:
    """Stage 3: provoke a validation error that names the real limit.

    Sends ``max_tokens`` far above any real cap with a two-token prompt. Servers reject
    this before inference, so the probe costs nothing; some gateways demand
    ``stream: true`` on every request, hence the second attempt.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": _ABSURD_MAX_TOKENS,
    }
    texts: list[str] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for stream in (False, True):
            resp = await client.post(
                url, headers=_auth_headers(api_key), json={**payload, "stream": stream}
            )
            body = resp.text or ""
            texts.append(body)
            # Retry as a stream only when the server rejected the non-streaming shape itself.
            if "stream" not in body.lower():
                break
    blob = "\n".join(texts)
    for pattern in _CONTEXT_PATTERNS:
        match = pattern.search(blob)
        if match:
            window = _sane(match.group(1))
            if window:
                return window, f"上游报错含明确窗口：{match.group(0).strip()[:120]}"
    for pattern in _OUTPUT_CAP_PATTERNS:
        match = pattern.search(blob)
        if match:
            # Output cap only — adopting it would under-report on every vendor whose
            # output cap is smaller than its context window (Anthropic, DashScope…).
            return 0, (
                f"上游只回报了 max_tokens 上限 {match.group(1)}，"
                "那通常是输出上限而非上下文窗口，未采纳"
            )
    return 0, "上游报错未包含可识别的窗口信息"


def from_name_heuristic(model_name: str) -> tuple[int, str]:
    """Stage 4: guess from the model family. Lowest trust, never overwrites a real value."""
    hay = (model_name or "").strip()
    if not hay:
        return 0, ""
    for pattern, window in NAME_WINDOW_RULES:
        if pattern.search(hay):
            return window, f"模型名匹配 /{pattern.pattern}/ → {window}"
    return 0, "模型名未命中任何已知模型族"


async def discover_context_length(
    *,
    provider: str,
    base_url: str,
    api_key: str,
    model_name: str,
    allow_error_probe: bool = False,
    allow_name_heuristic: bool = True,
    timeout: int = 15,
) -> ContextProbeResult:
    """Discover ``model_name``'s context window, best source first.

    ``allow_error_probe`` gates the stage that issues a real (rejected) chat request; it
    is off for the implicit fill-in on save and on for an explicit "detect" click.
    ``allow_name_heuristic`` gates the family table — callers that would rather store
    nothing than store a guess turn it off.

    Never raises: a provider that is down or refuses the probe yields an empty result
    with the reason in ``notes``, because discovery is an optional convenience on the
    configuration path, never a precondition for saving.
    """
    result = ContextProbeResult()
    name = (model_name or "").strip()
    if not name:
        result.notes.append("模型名为空，无法探测")
        return result

    from core.llm.providers.registry import get_spec

    try:
        spec = get_spec(provider)
    except Exception:  # noqa: BLE001 — unknown vendor id: treat as OpenAI-compatible
        spec = None
    is_ollama = (getattr(spec, "id", "") or provider) == "ollama"

    stages: list[tuple[str, Any]] = []
    if base_url:
        if is_ollama:
            stages.append(("ollama_show", lambda: _from_ollama_show(base_url, name, timeout)))
        stages.append(
            ("models_endpoint", lambda: _from_models_endpoint(base_url, api_key, name, timeout))
        )
        if allow_error_probe and not is_ollama:
            stages.append(
                (
                    "max_tokens_probe",
                    lambda: _from_max_tokens_probe(base_url, api_key, name, timeout),
                )
            )
    else:
        result.notes.append("未配置 base_url，跳过所有联网探测")

    for source, run in stages:
        label = SOURCE_LABELS.get(source, source)
        try:
            window, detail = await run()
        except Exception as exc:  # noqa: BLE001 — every stage is best-effort
            result.notes.append(f"{label}：探测失败（{type(exc).__name__}）")
            logger.debug("[context_probe] %s failed for %s: %s", source, name, exc)
            continue
        if window:
            result.context_length = window
            result.source = source
            result.confidence = "medium" if source == "max_tokens_probe" else "high"
            result.detail = detail
            result.notes.append(f"{label}：{detail}")
            return result
        result.notes.append(f"{label}：{detail}")

    if allow_name_heuristic:
        window, detail = from_name_heuristic(name)
        if window:
            result.context_length = window
            result.source = "name_heuristic"
            result.confidence = "low"
            result.detail = detail
            result.notes.append(f"{SOURCE_LABELS['name_heuristic']}：{detail}")
            return result
        result.notes.append(f"{SOURCE_LABELS['name_heuristic']}：{detail}")

    return result
