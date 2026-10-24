"""Compose model-owned services at the application host boundary."""
from core.harness.registry.model_services import register_model_services
from core.llm import prompt_kinds, prompt_versions, skill_selection


def active_prompt_versions():
    versions = []
    for kind in list(prompt_kinds.KIND_SPECS):
        try:
            active = prompt_versions.get_active_version(kind)
        except Exception:
            continue
        if active:
            versions.append((kind, active))
    return versions


def register():
    from core.harness.registry.mcp_clients import register_mcp_client_factory
    from core.llm.mcp.lifetime import discover
    register_mcp_client_factory(discover)
    from core.models.config import ModelConfigService
    from core.llm.prompt_rendering import render_active_prompt_part
    from core.llm.runtime.review_text_agent import run_text_agent
    register_model_services(
        ranker=lambda **kwargs: skill_selection.select_skills_for_query(**kwargs),
        prompt_reader=active_prompt_versions,
        prompt_renderer=render_active_prompt_part,
        text_agent=run_text_agent,
        environment=lambda: ModelConfigService.get_instance().get_mcp_env_overlay(),
    )
