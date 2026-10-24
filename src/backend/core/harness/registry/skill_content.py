"""Installed integrations adapt their own skill text for the execution host."""
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass
class SkillContentAdapters:
    adapters: dict[str, Callable[[str, str | None], str]] = field(default_factory=dict)


@lru_cache(maxsize=1)
def skill_content_adapters() -> SkillContentAdapters:
    return SkillContentAdapters()


def register_skill_content_adapter(name, adapter):
    skill_content_adapters().adapters[name] = adapter


def adapt_skill_content(content: str, user_id: str | None) -> str:
    for adapter in tuple(skill_content_adapters().adapters.values()):
        content = adapter(content, user_id)
    return content
