"""Wrapped SDK diagnostics must never serialize exception payloads."""
from core.capabilities.errors import PermissionDenied
from core.llm.chat_models import _safe_exception_chain


def test_diagnostic_keeps_wrapped_authorization_code_without_private_payload():
    private = "SECRET_URL_HEADER_TOKEN"
    inner = PermissionDenied(private, details={"headers": private})
    outer = RuntimeError(private)
    outer.__cause__ = inner
    chain = _safe_exception_chain(outer)
    assert chain == [
        {"type": "builtins.RuntimeError"},
        {"type": "core.capabilities.errors.PermissionDenied", "code": "permission_denied"},
    ]
    assert private not in repr(chain)


def test_diagnostic_bounds_cycles_and_context_depth():
    first = ValueError("private")
    second = RuntimeError("private")
    first.__context__ = second
    second.__cause__ = first
    assert len(_safe_exception_chain(first)) == 2
    current = RuntimeError("private")
    for _ in range(20):
        parent = RuntimeError("private")
        parent.__cause__ = current
        current = parent
    assert len(_safe_exception_chain(current)) == 8
