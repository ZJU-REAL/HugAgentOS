"""LLM package.

Keep package import framework-neutral: pure context/manifest modules must be
usable in tests and offline tooling without importing AgentScope.  Legacy
attribute exports remain available lazily.
"""


def __getattr__(name):  # noqa: ANN001, ANN202
    if name in {"make_chat_model", "get_default_model"}:
        from core.llm import chat_models

        return getattr(chat_models, name)
    raise AttributeError(name)


__all__ = ["get_default_model", "make_chat_model"]
