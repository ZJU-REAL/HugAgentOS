"""Agent assembly phase: model selection. """

from __future__ import annotations

from dataclasses import replace

from core.llm.chat_models import get_default_model, make_chat_model
from core.llm.factory.request import AgentRequest


async def model_selection(request: AgentRequest, *, _elapsed, _log, _shared_subagent_cfg, cfg):
    default_model = None
    _subagent_model_pinned = False
    _selected_provider_cfg = None
    _selected_provider_id = (request.model_provider_id or "").strip()
    if _selected_provider_id:
        try:
            from core.services.model_config import ModelConfigService

            _selected_provider_cfg = (
                _shared_subagent_cfg
                or ModelConfigService.get_instance().resolve_provider(_selected_provider_id)
            )
            if _selected_provider_cfg:
                from core.llm.chat_models import build_model_for_mode
                from core.llm.failover import with_failover

                _mode = (request.chat_mode or "medium").lower()
                default_model = with_failover(
                    build_model_for_mode(_selected_provider_cfg, mode=_mode, stream=True),
                    _selected_provider_cfg,
                    mode=_mode,
                )
                _subagent_model_pinned = request.user_agent is not None
                _log.info(
                    "[factory] using selected model: %s",
                    _selected_provider_cfg.model_name,
                )
        except Exception as exc:
            _log.warning("[factory] selected model resolve failed: %s, falling back", exc)
    _mode_role = request.model_role or ("plan_agent" if request.plan_mode else None)
    if default_model is None and _mode_role:
        try:
            from core.services.model_config import ModelConfigService

            _mode_cfg = ModelConfigService.get_instance().resolve(_mode_role)
            if _mode_cfg:
                from core.llm.failover import with_failover

                default_model = with_failover(
                    make_chat_model(
                        model=_mode_cfg.model_name,
                        temperature=_mode_cfg.temperature,
                        max_tokens=_mode_cfg.max_tokens,
                        timeout=_mode_cfg.timeout,
                        base_url=_mode_cfg.base_url,
                        api_key=_mode_cfg.api_key,
                        provider=_mode_cfg.provider,
                        provider_extra=_mode_cfg.provider_extra,
                        api_protocol=(_mode_cfg.extra or {}).get("api_protocol"),
                        stream=True,
                    ),
                    _mode_cfg,
                )
                _log.info("[factory] using %s model: %s", _mode_role, _mode_cfg.model_name)
        except Exception as exc:
            _log.warning(
                "[factory] %s model resolve failed: %s, falling back to main_agent",
                _mode_role,
                exc,
            )
    if default_model is None:
        default_model = get_default_model(cfg.model, stream=True)

    # ── Sub-agent config override (model / temperature / max_tokens) ──
    # Triggers when user_agent specifies a custom model provider, a non-null
    # temperature, or a non-null max_tokens. The shared role takes precedence;
    # otherwise parameter-only overrides retain the user's selected provider.
    # Pin selected child models to avoid replacing their fresh, thread-local
    # client with a cached main-agent client in DynamicModelMiddleware.
    if request.user_agent is not None and _shared_subagent_cfg is None:
        _user_temp = (
            float(request.user_agent.temperature)
            if request.user_agent.temperature is not None
            else None
        )
        _user_max_tokens = request.user_agent.max_tokens or None
        _user_timeout = request.user_agent.timeout or None
        _user_provider_id = request.user_agent.model_provider_id

        if _user_provider_id or _user_temp is not None or _user_max_tokens or _user_timeout:
            try:
                from core.llm.chat_models import build_model_for_mode
                from core.services.model_config import ModelConfigService

                _model_service = ModelConfigService.get_instance()
                _provider_cfg = (
                    _model_service.resolve_provider(_user_provider_id)
                    if _user_provider_id
                    else None
                )
                _base_cfg = (
                    _provider_cfg or _selected_provider_cfg or _model_service.resolve("main_agent")
                )
                if _base_cfg is not None:
                    _override_cfg = replace(
                        _base_cfg,
                        temperature=(
                            _user_temp if _user_temp is not None else _base_cfg.temperature
                        ),
                        max_tokens=_user_max_tokens or _base_cfg.max_tokens,
                        timeout=_user_timeout or _base_cfg.timeout,
                    )
                    from core.llm.failover import with_failover

                    _overrides = {
                        key: value
                        for key, value in {
                            "temperature": _user_temp,
                            "max_tokens": _user_max_tokens,
                            "timeout": _user_timeout,
                        }.items()
                        if value is not None
                    }
                    _mode = (request.chat_mode or "medium").lower()
                    default_model = with_failover(
                        build_model_for_mode(_override_cfg, mode=_mode, stream=True),
                        _override_cfg,
                        mode=_mode,
                        parameter_overrides=_overrides,
                    )
                    # Parameter overrides must survive on_reply too. The model
                    # is freshly constructed in this child's event loop.
                    _subagent_model_pinned = True
                    request.model_provider_id = _override_cfg.provider_id
                    _log.info(
                        "[factory] subagent config override: model=%s, temp=%s, max_tokens=%s",
                        _override_cfg.model_name,
                        _override_cfg.temperature,
                        _override_cfg.max_tokens,
                    )
            except Exception as exc:
                _log.warning("[factory] subagent config override failed: %s, using default", exc)

    if request.run_id:
        from core.llm.model_usage import instrument_model_usage

        instrument_model_usage(default_model)

    _log.info("[factory] +%s model created", _elapsed())

    return (
        _subagent_model_pinned,
        default_model,
    )
