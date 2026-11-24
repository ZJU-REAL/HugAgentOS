"""Prompt and model-default configuration.

This module is intentionally stdlib-only (JSON) to avoid extra deps.

Config lookup order:
1) env var JX_PROMPT_CONFIG
2) repo default: prompts/config/default.json
3) built-in defaults (always available)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# repo default: <backend_root>/prompts/config/default.json (next to this module)
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "default.json"


@dataclass(frozen=True)
class SystemPromptConfig:
    # filesystem|inline
    # - filesystem: load from prompt_dir (or env PROMPT_DIR)
    # - inline: use inline_template (or env PROMPT_INLINE_TEMPLATE)
    provider: str = "filesystem"
    # Directory containing prompt pack files.
    prompt_dir: str = "./prompts/prompt_text/default"
    # Optional list of prompt parts (prompt_ids) to concatenate for the system prompt.
    # Each part is resolved by FilesystemPromptProvider using the same naming convention:
    #   <prompt_dir>/<prompt_id>.system.(md|txt)
    # Example part ids: "system/00_time_role", "system/40_flow".
    parts: List[str] = field(default_factory=list)
    inline_template: str = ""


@dataclass(frozen=True)
class ModelConfig:
    temperature: float = 0.6
    # None = 不限制输出长度，交给模型供应商自己的默认值。
    max_tokens: Optional[int] = None
    timeout: int = 120


@dataclass(frozen=True)
class PromptConfig:
    version: int = 1
    system_prompt: SystemPromptConfig = field(default_factory=SystemPromptConfig)
    model: ModelConfig = field(default_factory=ModelConfig)


def _coerce_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _load_json_file(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


_config_cache: Optional[Tuple[str, float, "PromptConfig"]] = None


def load_prompt_config(path: Optional[str] = None) -> PromptConfig:
    """Load PromptConfig with validation + defaults.

    Results are cached based on file mtime to avoid re-reading on every request.
    This function must never raise due to missing files; it falls back to defaults.
    """
    global _config_cache

    config_path = (
        Path(path)
        if path
        else Path(os.getenv("JX_PROMPT_CONFIG", str(DEFAULT_CONFIG_PATH)))
    )

    # Check mtime cache
    try:
        current_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
    except OSError:
        current_mtime = 0.0

    cache_key = str(config_path)
    if _config_cache is not None:
        cached_path, cached_mtime, cached_config = _config_cache
        if cached_path == cache_key and cached_mtime == current_mtime:
            return cached_config

    raw: Dict[str, Any] = {}
    try:
        if config_path.exists():
            raw = _load_json_file(config_path)
    except Exception:
        raw = {}

    version = int(raw.get("version", 1)) if isinstance(raw.get("version", 1), int | str) else 1

    sp_raw = _coerce_dict(raw.get("system_prompt"))
    provider = str(sp_raw.get("provider", "")).strip().lower() or "filesystem"

    # prompt_dir can also be overridden by env PROMPT_DIR at runtime,
    # but we keep a config-level default to support multiple prompt packs.
    prompt_dir = str(sp_raw.get("prompt_dir", sp_raw.get("dir", "./prompts/prompt_text/default")))

    parts_raw = sp_raw.get("parts")
    parts: List[str] = []
    if isinstance(parts_raw, list) and all(isinstance(x, str) for x in parts_raw):
        parts = list(parts_raw)

    system_prompt = SystemPromptConfig(
        provider=provider,
        prompt_dir=prompt_dir,
        parts=parts,
        inline_template=str(sp_raw.get("inline_template", "")),
    )

    model_raw = _coerce_dict(raw.get("model"))
    model = ModelConfig(
        temperature=float(model_raw.get("temperature", ModelConfig().temperature)),
        max_tokens=int(_max_tokens) if (_max_tokens := model_raw.get("max_tokens")) else None,
        timeout=int(model_raw.get("timeout", ModelConfig().timeout)),
    )

    result = PromptConfig(
        version=version,
        system_prompt=system_prompt,
        model=model,
    )

    _config_cache = (cache_key, current_mtime, result)
    return result
