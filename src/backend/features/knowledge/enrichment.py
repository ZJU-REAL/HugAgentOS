"""enrichment responsibilities."""
from __future__ import annotations
import os
import re
import logging
logger = logging.getLogger(__name__)

def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks and markdown formatting from LLM output."""
    # Remove <think>...</think> blocks (reasoning model output)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    # Remove markdown bold/italic markers
    text = re.sub(r'\*+', '', text)
    # Remove markdown headers
    text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
    # Remove bullet points
    text = re.sub(r'^[\-\*]\s*', '', text, flags=re.MULTILINE)
    return text.strip()


def resolve_main_endpoint():
    """main_agent 端点，带上它说的协议——KB 的两处抽取都得走对那条线。"""
    from core.models.completions.single_turn import Endpoint

    try:
        from core.models.config import ModelConfigService

        cfg = ModelConfigService.get_instance().resolve("main_agent")
        if cfg and cfg.base_url and cfg.model_name:
            return Endpoint.from_resolved(cfg)
    except Exception:
        pass
    base_url, api_key, model_name = resolve_main_model_config()
    return Endpoint(base_url=base_url, api_key=api_key, model_name=model_name)


def resolve_main_model_config() -> tuple[str, str, str]:
    """Resolve main_agent model config from DB, with env fallback."""
    try:
        from core.models.config import ModelConfigService
        cfg = ModelConfigService.get_instance().resolve("main_agent")
        if cfg:
            return cfg.base_url.rstrip("/"), cfg.api_key, cfg.model_name
    except Exception:
        pass
    return (
        os.getenv("MODEL_URL", "").rstrip("/"),
        os.getenv("API_KEY", ""),
        os.getenv("BASE_MODEL_NAME", ""),
    )


def call_main_model(
    *, system: str, user: str, temperature: float, max_tokens: int, timeout: int = 30
) -> str:
    """跑一次主模型补全；未配置时返回空串，调用方按「这一步没产出」处理。"""
    import requests as _requests

    from core.models.completions.single_turn import build_request, parse_response, turns

    endpoint = resolve_main_endpoint()
    if not endpoint.base_url or not endpoint.model_name:
        return ""
    url, headers, payload = build_request(
        endpoint, turns(user, system), temperature=temperature, max_tokens=max_tokens
    )
    resp = _requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    return parse_response(endpoint, resp.json()).text


def extract_keywords_llm(content: str, count: int) -> list[str]:
    """Call configured LLM to extract keywords from a text chunk."""
    try:
        raw = strip_think_tags(
            call_main_model(
                system=(
                    "你是关键词提取工具。"
                    "输出规则：只输出关键词，用英文逗号分隔，不要编号，不要解释，不要分析过程。"
                    "示例输出：数字化转型,产业升级,人工智能"
                ),
                user=f"从以下文本提取{count}个关键词，直接输出逗号分隔的关键词：\n\n{content[:2000]}",
                temperature=0.1,
                max_tokens=200,
            )
        )
        lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
        best_line = raw
        for line in reversed(lines):
            if ("," in line or "，" in line) and len(line) < 500:
                best_line = line
                break
        keywords = [kw.strip() for kw in best_line.replace("，", ",").split(",") if kw.strip() and len(kw.strip()) < 30]
        return keywords[:count]
    except Exception as exc:
        logger.warning("LLM keyword extraction failed: %s", exc)
        return []


def generate_questions_llm(content: str, count: int) -> list[str]:
    """Call configured LLM to generate retrieval questions for a text chunk."""
    try:
        raw = strip_think_tags(
            call_main_model(
                system=(
                    "你是问题生成工具。"
                    "输出规则：每行一个问题，不要编号，不要解释，不要分析过程。直接输出问题本身。"
                    "示例输出：\n什么是数字化转型？\n如何申报产业升级项目？"
                ),
                user=f"根据以下文本生成{count}个检索问题，每行一个：\n\n{content[:2000]}",
                temperature=0.3,
                max_tokens=500,
            )
        )
        questions = []
        for q in raw.split("\n"):
            q = q.strip().lstrip("0123456789.、）)-— ").strip()
            if not q or len(q) < 5:
                continue
            if q.startswith(("分析", "角色", "任务", "输出", "规则", "示例")):
                continue
            questions.append(q)
        return questions[:count]
    except Exception as exc:
        logger.warning("LLM question generation failed: %s", exc)
        return []
