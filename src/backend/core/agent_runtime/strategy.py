"""Routing strategy selection.

Default behavior is safe-by-default: always route to `main`.

Env:
- ROUTER_STRATEGY=main_only|llm_router (default: main_only)

Notes:
- `llm_router` is a placeholder for future work; it currently falls back to main.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol

from core.config.settings import settings


class RouterStrategy(Protocol):
    def route(self, user_input: str, context: Dict[str, Any] | None = None) -> str: ...


@dataclass(frozen=True)
class MainOnlyStrategy:
    def route(self, user_input: str, context: Dict[str, Any] | None = None) -> str:
        return "main"


def get_router_strategy() -> RouterStrategy:
    name = settings.routing.strategy

    # Placeholder: future LLM router (must remain safe + fallback to main).
    if name == "llm_router":
        return MainOnlyStrategy()

    return MainOnlyStrategy()
