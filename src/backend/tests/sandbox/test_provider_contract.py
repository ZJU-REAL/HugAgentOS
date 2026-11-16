"""Every real sandbox provider satisfies its contract on its own.

The providers do not subclass the Protocols in ``core.sandbox.protocol``, so
nothing a Protocol says reaches them at runtime: a member the Protocol declares
but a provider forgets fails only at the first call site that reads it, in
production. Call-site tests hand in fake providers, which hid exactly that gap
(the agent API ``runs_on_host`` AttributeError on ``TeamOpenSandboxProvider``).
This test checks the real classes, and finds them by scanning the source, so a
new provider cannot opt out by not being listed.
"""

from __future__ import annotations

import ast
import functools
import importlib
from pathlib import Path

import pytest

from core.sandbox.protocol import OrdinarySandboxProvider, SandboxAdmin, SandboxProvider

BACKEND = Path(__file__).resolve().parents[2]
# Packages that hold runtime code; extensions/ ships packaged resources
# (templates with .py names that are not Python) and never defines a provider.
SCANNED = (
    "api", "core", "features", "integrations", "orchestration", "personal_agent", "edition_ee",
)
# Not providers: the router forwards to one.
NOT_PROVIDERS = {("core.sandbox.sessions.router", "SessionSandboxRouter")}
# What ``SANDBOX_PROVIDER`` can select (core.sandbox.factory). These and their
# subclasses also serve the security console's admin view.
ORDINARY_ROOTS = (
    "core.sandbox.providers.opensandbox.provider.OpenSandboxProvider",
    "core.sandbox.providers.cube.provider.CubeSandboxProvider",
    "core.sandbox.providers.script_runner.provider.ScriptRunnerProvider",
)
# Subclasses that inherit start_process instead of defining it. The edition
# seam resolves to the EE TeamOpenSandboxProvider or the CE overlay class.
SUBCLASS_MODULES = ("core.sandbox.providers.edition", "personal_agent.computer.sandbox")


def _members(protocol) -> set[str]:
    own = set()
    for klass in protocol.__mro__:
        if klass is object or klass.__name__ in {"Protocol", "Generic"}:
            continue
        own |= {n for n in getattr(klass, "__annotations__", {}) if not n.startswith("_")}
        own |= {n for n, v in vars(klass).items() if callable(v) and not n.startswith("_")}
    return own


def _scanned_classes() -> list[tuple[str, str]]:
    """Every non-test class that defines ``start_process`` is a provider."""
    found = []
    tops = (BACKEND / top for top in SCANNED)
    for path in sorted(p for top in tops if top.is_dir() for p in top.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if "__pycache__" in rel.parts:
            continue
        module = ".".join(rel.with_suffix("").parts)
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.ClassDef) or (module, node.name) in NOT_PROVIDERS:
                continue
            if isinstance(node, ast.ClassDef) and any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == "start_process"
                for item in node.body
            ):
                found.append((module, node.name))
    return found


def _subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _subclasses(sub)


def _qualname(cls) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


@functools.cache
def _providers() -> dict[str, type]:
    roots = [
        getattr(importlib.import_module(m), n)
        for m, n in _scanned_classes()
        if m != "core.sandbox.protocol"
    ]
    for module in SUBCLASS_MODULES:
        importlib.import_module(module)
    found = {}
    for root in roots:
        for cls in (root, *_subclasses(root)):
            if not cls.__module__.startswith("tests"):
                found[_qualname(cls)] = cls
    return found


def _is_ordinary(cls) -> bool:
    return any(_qualname(k) in ORDINARY_ROOTS for k in cls.__mro__)


def test_scan_finds_the_known_providers():
    names = set(_providers())
    # CE ships without the Cube provider; require only the roots it has.
    shipped = {
        root for root in ORDINARY_ROOTS
        if (BACKEND / (root.rsplit(".", 1)[0].replace(".", "/") + ".py")).is_file()
    }
    assert "core.sandbox.providers.opensandbox.provider.OpenSandboxProvider" in shipped
    assert shipped <= names
    from core.sandbox.providers.edition import PersistentSandboxProvider

    assert {
        "features.evaluation.sandbox_provider.provider.EvaluationSandboxProvider",
        "personal_agent.computer.sandbox.PersonalSandboxProvider",
        _qualname(PersistentSandboxProvider),
    } <= names


@pytest.mark.parametrize("qualname", sorted(_providers()))
def test_provider_declares_its_whole_contract(qualname):
    cls = _providers()[qualname]
    contract = OrdinarySandboxProvider if _is_ordinary(cls) else SandboxProvider
    missing = sorted(m for m in _members(contract) if not hasattr(cls, m))
    assert not missing, f"{qualname} does not declare {missing}"


@pytest.mark.parametrize("qualname", sorted(_providers()))
def test_runs_on_host_is_an_explicit_bool(qualname):
    # Whether a command runs without a container boundary is declared by the
    # provider, never inferred from a fallback at the call site.
    assert isinstance(_providers()[qualname].runs_on_host, bool)


def test_protocols_carry_no_defaults():
    # A Protocol default never reaches a class that does not subclass the
    # Protocol, so it would only document a value no provider has.
    for protocol in (SandboxProvider, SandboxAdmin, OrdinarySandboxProvider):
        data = [n for n in protocol.__annotations__ if not n.startswith("_")]
        assert [n for n in data if n in vars(protocol)] == []


def test_router_carries_the_ordinary_data_members():
    from core.sandbox.sessions.router import SessionSandboxRouter

    class Ordinary:
        name = "opensandbox"
        runs_on_host = False

    router = SessionSandboxRouter(Ordinary())
    assert (router.name, router.runs_on_host) == ("opensandbox", False)


def test_agent_api_accepts_the_real_persistent_provider(monkeypatch):
    # The production path that raised AttributeError: the agent API sandbox
    # gate reads runs_on_host through the router on the real provider the
    # factory builds for SANDBOX_PROVIDER=opensandbox (EE: TeamOpenSandboxProvider).
    import core.sandbox as sandbox
    from core.llm.api_runtime import agent_tools
    from core.sandbox.providers.edition import PersistentSandboxProvider
    from core.sandbox.sessions.router import SessionSandboxRouter

    router = SessionSandboxRouter(object.__new__(PersistentSandboxProvider))
    monkeypatch.setattr(sandbox, "get_sandbox_provider", lambda: router)
    monkeypatch.setenv("SANDBOX_TOOLS_ENABLED", "true")
    assert agent_tools._provider() is router
