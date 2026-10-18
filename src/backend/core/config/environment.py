"""Centralized application settings.

All environment variables are read here once at import time.
Other modules should ``from core.config.settings import settings`` instead
of calling ``os.getenv()`` directly.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _load_env_files() -> None:
    """Load repo-level env files without overriding real process env vars.

    Precedence:
    1. Existing process environment
    2. Env-specific file such as .env.dev
    3. Base .env
    """
    base_env_path = _REPO_ROOT / ".env"
    base_values = dotenv_values(base_env_path) if base_env_path.exists() else {}

    resolved_env = (
        (
            os.getenv("ENV")
            or os.getenv("ENVIRONMENT")
            or str(base_values.get("ENV") or "")
            or str(base_values.get("ENVIRONMENT") or "")
        )
        .strip()
        .lower()
    )

    candidate_paths = [base_env_path]
    if resolved_env:
        candidate_paths.append(_REPO_ROOT / f".env.{resolved_env}")
    elif (_REPO_ROOT / ".env.dev").exists():
        candidate_paths.append(_REPO_ROOT / ".env.dev")

    merged_values: dict[str, str] = {}
    for path in candidate_paths:
        if not path.exists():
            continue
        for key, value in dotenv_values(path).items():
            if value is not None:
                merged_values[key] = value

    for key, value in merged_values.items():
        os.environ.setdefault(key, value)


_load_env_files()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes")


def _int(val: str, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _float(val: str, default: float = 0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# Legacy frontend model alias, stamped on runs/requests when no explicit model
# name is resolved. Runtime model selection is DB-driven (model providers /
# role assignments); this is only the last-resort label. Single source of
# truth — do not inline "qwen" elsewhere.
DEFAULT_CHAT_MODEL_ALIAS = "qwen"
