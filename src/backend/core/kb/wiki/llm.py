"""Wiki 管线的 LLM 调用层。

三件事在这里统一处理，别在各阶段各写一份：

1. **模型解析**：优先用模型管理里绑定给 ``kb_wiki`` 角色的供应商，未绑定回落
   ``main_agent``，再回落环境变量。Wiki 是离线批处理、调用量随文档量线性增长，
   单独绑一个便宜模型是常态用法。
2. **JSON 健壮性**：推理模型会吐 ``<think>``、会加 ```json 围栏、会在字符串里塞
   字面换行。这些都在这里修掉——单次解析失败不能让整个作业挂掉。
3. **重试与限额**：区分瞬时错误（超时/429/5xx，值得重试）与永久错误（400 类，
   重试也没用），并对单个作业的调用总次数封顶。
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests
from core.llm._distill_shared import parse_strict_json, strip_think_blocks

logger = logging.getLogger(__name__)

# 单次调用超时。写页阶段的输出可能上千字，给得比常规工具调用宽
_DEFAULT_TIMEOUT = 180.0
_MAX_ATTEMPTS = 3


class WikiLLMError(RuntimeError):
    """LLM 调用最终失败（已用尽重试，或遇到不可重试的错误）。"""


class WikiQuotaExceeded(RuntimeError):
    """本作业的 LLM 调用次数已达上限。"""


@dataclass
class _ModelEndpoint:
    base_url: str
    api_key: str
    model_name: str


def _resolve_endpoint() -> _ModelEndpoint:
    """kb_wiki 角色 → main_agent 角色 → 环境变量。

    只有 ``kb_wiki`` 这一层是 Wiki 特有的，后面两级回落复用
    ``kb_processing.resolve_main_model_config``——同一个知识库子系统里，向量化与
    Wiki 生成对「主模型在哪」必须给出同一个答案。
    """
    try:
        from core.services.model_config import ModelConfigService

        cfg = ModelConfigService.get_instance().resolve("kb_wiki")
        if cfg and cfg.base_url and cfg.model_name:
            return _ModelEndpoint(cfg.base_url.rstrip("/"), cfg.api_key, cfg.model_name)
    except Exception as exc:  # noqa: BLE001 - 配置服务不可用时退到主模型，不阻断管线
        logger.warning("Wiki 模型角色解析失败，回落主模型: %s", exc)

    from core.content.kb_processing import resolve_main_model_config

    return _ModelEndpoint(*resolve_main_model_config())


def wiki_model_configured() -> bool:
    endpoint = _resolve_endpoint()
    return bool(endpoint.base_url and endpoint.model_name)


class LLMBudget:
    """一个作业的调用预算，跨阶段共享一个实例。"""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max(1, int(max_calls))
        self.calls = 0
        self.prompt_chars = 0
        self.completion_chars = 0

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise WikiQuotaExceeded(f"本次 Wiki 生成的模型调用次数已达上限 {self.max_calls}")

    def record(self, prompt_chars: int, completion_chars: int) -> None:
        self.calls += 1
        self.prompt_chars += prompt_chars
        self.completion_chars += completion_chars

    def snapshot(self) -> Dict[str, int]:
        return {
            "llm_calls": self.calls,
            "prompt_chars": self.prompt_chars,
            "completion_chars": self.completion_chars,
        }


def _is_transient(exc: Exception) -> bool:
    """瞬时错误值得重试；参数/鉴权类错误重试多少次都一样。"""
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        return status == 429 or status >= 500
    return False


def _repair_literal_newlines(text: str) -> str:
    """把 JSON 字符串值内部的字面换行/制表符转义掉。

    提示词已经禁止过，但模型仍会偶发违反，而这恰好是最常见的一种解析失败。逐字符
    扫描并跟踪是否处在字符串里，只改字符串内部的控制字符。这一段是本模块相对
    仓库通用解析器**额外**补的一层，其余能力（think 剥离、围栏、括号配平提取）
    一律走 ``core.llm._distill_shared``。
    """
    out = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch == "\n":
            out.append("\\n")
            continue
        if in_string and ch == "\r":
            continue
        if in_string and ch == "\t":
            out.append("\\t")
            continue
        out.append(ch)
    return "".join(out)


def parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """尽最大努力把模型输出解析成 dict；实在解析不出返回 None。

    调用方一律要处理 None——把它当作"这一步没产出"降级继续，而不是抛异常，
    否则一篇文档的格式抽风会连累整批。
    """
    if not raw:
        return None
    parsed = parse_strict_json(raw) or parse_strict_json(_repair_literal_newlines(raw))
    if parsed is None:
        logger.warning("Wiki LLM 输出无法解析为 JSON（前 200 字）: %s", raw[:200])
    return parsed


def call_llm(
    *,
    system: Optional[str],
    user: str,
    budget: Optional[LLMBudget] = None,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    timeout: float = _DEFAULT_TIMEOUT,
    purpose: str = "wiki",
) -> str:
    """跑一次对话补全，返回剥掉思考段的正文。

    失败会抛 :class:`WikiLLMError`；调用方决定是让整篇文档降级还是让作业失败。
    """
    if budget is not None:
        budget.check()

    endpoint = _resolve_endpoint()
    if not endpoint.base_url or not endpoint.model_name:
        raise WikiLLMError(
            "未配置 Wiki 生成模型：请在模型管理中为「知识库 Wiki 实体抽取」角色绑定供应商"
        )

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})

    payload = {
        "model": endpoint.model_name,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {endpoint.api_key}",
        "Content-Type": "application/json",
    }

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = requests.post(
                f"{endpoint.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            body = response.json()
            content = str(
                ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            )
            cleaned = strip_think_blocks(content)
            if budget is not None:
                budget.record(len(system or "") + len(user), len(content))
            return cleaned
        except Exception as exc:  # noqa: BLE001 - 统一分类后决定是否重试
            last_exc = exc
            if not _is_transient(exc) or attempt == _MAX_ATTEMPTS:
                break
            backoff = min(2**attempt, 8) + random.uniform(0, 0.5)
            logger.warning(
                "Wiki LLM 调用失败（%s，第 %d/%d 次），%.1fs 后重试: %s",
                purpose,
                attempt,
                _MAX_ATTEMPTS,
                backoff,
                exc,
            )
            time.sleep(backoff)

    raise WikiLLMError(f"Wiki 模型调用失败（{purpose}）: {last_exc}")


def call_llm_json(
    *,
    system: Optional[str],
    user: str,
    budget: Optional[LLMBudget] = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    purpose: str = "wiki",
) -> Optional[Dict[str, Any]]:
    """跑一次调用并解析成 dict；调用或解析失败都返回 None。"""
    try:
        raw = call_llm(
            system=system,
            user=user,
            budget=budget,
            temperature=temperature,
            max_tokens=max_tokens,
            purpose=purpose,
        )
    except WikiQuotaExceeded:
        raise
    except WikiLLMError as exc:
        logger.warning("Wiki 阶段 %s 调用失败，按空结果降级: %s", purpose, exc)
        return None
    return parse_json_object(raw)


def split_summary_line(text: str) -> tuple[str, str]:
    """拆出 ``SUMMARY: ...`` 首行与其后的正文。

    模型偶尔会忘了这一行，此时把整段当正文、摘要留空，由调用方用首个非标题
    段落兜底——总好过把正文第一行错当摘要展示在索引里。
    """
    cleaned = strip_think_blocks(text or "").lstrip()
    if not cleaned:
        return "", ""
    first, _, rest = cleaned.partition("\n")
    match = re.match(r"^\s*SUMMARY\s*[:：]\s*(.*)$", first, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip(), rest.strip()
    return "", cleaned
