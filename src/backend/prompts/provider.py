"""PromptProvider: pluggable prompt loading with safe fallbacks.

Design goals:
- No import-time dependency on external services / keys.
- Filesystem → Inline → Hardcoded minimal fallback.
- Strict/loose formatting controlled by env PROMPT_STRICT_VARS.

Env:
- PROMPT_PROVIDER: filesystem|inline (default: filesystem)
- PROMPT_DIR: directory for filesystem prompts (default: ./prompts/prompt_text/default)
- PROMPT_INLINE_TEMPLATE: inline template string (optional)
- PROMPT_STRICT_VARS: 1|0 (default: 1)

Filesystem convention (minimal):
- {prompt_id}.{role}.txt (preferred)
- {prompt_id}.{role}.md
- {prompt_id}.txt (fallback)
- {prompt_id}.md

For system prompt, use prompt_id="system", role="system".
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Protocol


# ── File content cache (mtime-based) ───────────────────────────────────────
# key = file path string, value = (mtime, content)
_FILE_CONTENT_CACHE: Dict[str, tuple[float, str]] = {}


def _read_file_cached(p: Path) -> str:
    """Read file contents with an mtime-based cache to avoid redundant disk I/O."""
    key = str(p)
    try:
        current_mtime = p.stat().st_mtime
    except OSError:
        # File disappeared – evict cache entry and return empty.
        _FILE_CONTENT_CACHE.pop(key, None)
        return ""

    cached = _FILE_CONTENT_CACHE.get(key)
    if cached is not None:
        cached_mtime, cached_content = cached
        if cached_mtime == current_mtime:
            return cached_content

    content = p.read_text(encoding="utf-8")
    _FILE_CONTENT_CACHE[key] = (current_mtime, content)
    return content


class PromptProvider(Protocol):
    def get_prompt(self, prompt_id: str, role: str, vars: Dict[str, Any] | None = None) -> str: ...


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


class _LooseFormatDict(dict):
    def __missing__(self, key: str) -> str:
        # Keep the placeholder visible (so missing vars are not silently swallowed)
        return "{" + key + "}"


def render_template(template: str, vars: Dict[str, Any] | None, strict: bool) -> str:
    vars = vars or {}
    if strict:
        return template.format(**vars)
    return template.format_map(_LooseFormatDict(vars))


def hardcoded_minimal_system_prompt() -> str:
    # Seam C7: the fallback prompt's branding comes from settings.branding (neutral by default in code;
    # the concrete brand is injected via env / the prompt_versions DB).
    from core.config.settings import settings

    brand = settings.branding
    if brand.org_name:
        intro = f"你是由{brand.org_name}开发的{brand.product_name}。\n"
    else:
        intro = f"你是{brand.product_name}。\n"
    return (
        intro
        + "请用专业、可核验的方式回答问题；需要数据/依据时优先使用可用工具检索。\n"
        + "若缺少关键上下文，请先询问澄清。"
    )


@dataclass(frozen=True)
class FilesystemPromptProvider:
    prompt_dir: Path
    strict_vars: bool = True

    def _candidate_paths(self, prompt_id: str, role: str) -> list[Path]:
        exts = [".txt", ".md"]
        candidates: list[Path] = []
        for ext in exts:
            candidates.append(self.prompt_dir / f"{prompt_id}.{role}{ext}")
        for ext in exts:
            candidates.append(self.prompt_dir / f"{prompt_id}{ext}")
        return candidates

    def get_prompt(self, prompt_id: str, role: str, vars: Dict[str, Any] | None = None) -> str:
        template = ""
        for p in self._candidate_paths(prompt_id, role):
            try:
                if p.exists():
                    template = _read_file_cached(p)
                    break
            except Exception:
                continue

        if not template.strip():
            return hardcoded_minimal_system_prompt() if prompt_id == "system" else ""

        return render_template(template, vars=vars, strict=self.strict_vars).strip()


@dataclass(frozen=True)
class InlinePromptProvider:
    template: str
    strict_vars: bool = True

    def get_prompt(self, prompt_id: str, role: str, vars: Dict[str, Any] | None = None) -> str:
        if not self.template.strip():
            return hardcoded_minimal_system_prompt() if prompt_id == "system" else ""
        return render_template(self.template, vars=vars, strict=self.strict_vars).strip()
