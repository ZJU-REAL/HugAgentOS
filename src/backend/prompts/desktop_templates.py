"""Versioned desktop templates. Facts and selection stay in runtime code."""
from contextlib import contextmanager
from contextvars import ContextVar
from prompts.provider import render_template

_override = ContextVar("desktop_prompt_version", default=None)


def desktop_parts():
    from core.services import prompt_version_service as pvs
    version = _override.get()
    return version.get("parts", []) if version is not None else pvs.effective_parts("desktop")


def render_desktop_part(part_id, **variables):
    part = next((p for p in desktop_parts() if p.get("part_id") == part_id), None)
    if not part or not part.get("is_enabled", True):
        return ""
    return render_template(part.get("content", ""), vars=variables, strict=False).strip()


@contextmanager
def desktop_version(version):
    token = _override.set(version)
    try:
        yield
    finally:
        _override.reset(token)
