"""Host-provided model operations used by capability and evolution domains."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable


def register_model_services(*, ranker: Callable[..., list[str]], prompt_reader: Callable[[], list[tuple[str, dict[str, Any]]]], prompt_renderer: Callable, text_agent: Callable, environment: Callable) -> None:
    pass
    registration_state().ranker = ranker
    registration_state().prompt_reader = prompt_reader
    registration_state().prompt_renderer = prompt_renderer
    registration_state().text_agent = text_agent
    registration_state().environment = environment


def rank_skills(**kwargs: Any) -> list[str]:
    if registration_state().ranker is None:
        raise RuntimeError("Model services are not registered")
    return registration_state().ranker(**kwargs)


def active_prompt_versions() -> list[tuple[str, dict[str, Any]]]:
    if registration_state().prompt_reader is None:
        raise RuntimeError("Model services are not registered")
    return registration_state().prompt_reader()


def render_prompt_part(kind, part):
    if registration_state().prompt_renderer is None:
        raise RuntimeError("Prompt rendering service is not registered")
    return registration_state().prompt_renderer(kind, part)


async def run_text_agent(prompt, **context):
    if registration_state().text_agent is None:
        raise RuntimeError("Text review agent is not registered")
    return await registration_state().text_agent(prompt, **context)


def model_environment():
    if registration_state().environment is None:
        raise RuntimeError("Model configuration environment is not registered")
    return registration_state().environment()


@dataclass
class RegistrationState:
    ranker: Callable[..., list[str]] | None = None
    prompt_reader: Callable[[], list[tuple[str, dict[str, Any]]]] | None = None
    prompt_renderer: Callable | None = None
    text_agent: Callable | None = None
    environment: Callable | None = None


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
