import importlib.util
from pathlib import Path

from scripts.refactor import import_contract as contract
ALIASES = {"core.services.market": "features.marketplace.source", "core.plugins.archive": "core.capabilities.plugins.archive", "core.services.other": "features.marketplace.other"}


def test_future_external_direct_private_import():
    assert contract.private_imports_in_module("core/services/market.py", "from core.capabilities.plugins.archive import _extract", ALIASES) == {"private-symbol: features.marketplace.source -> core.capabilities.plugins.archive._extract"}


def test_module_alias_cannot_hide_private_access():
    for source in ("import core.capabilities.plugins.archive as archive; archive._extract()", "from core.capabilities.plugins import archive as archive; archive._extract()", "import core.capabilities.plugins.archive; core.capabilities.plugins.archive._extract()"):
        assert contract.private_imports_in_module("core/services/market.py", source, ALIASES) == {"private-symbol: features.marketplace.source -> core.capabilities.plugins.archive._extract"}


def test_relative_target_resolves_before_ownership():
    assert contract.private_imports_in_module("core/services/market.py", "from ..plugins.archive import _extract", ALIASES) == {"private-symbol: features.marketplace.source -> core.capabilities.plugins.archive._extract"}


def test_future_feature_target_is_not_a_core_private_import():
    assert not contract.private_imports_in_module("api/host.py", "from core.services.other import _helper", ALIASES)


def test_internal_core_and_public_interfaces_are_allowed():
    assert not contract.private_imports_in_module("core/internal.py", "from core.capabilities.plugins.archive import _extract", ALIASES)
    assert not contract.private_imports_in_module("core/services/market.py", "from core.capabilities.plugins.archive import extract", ALIASES)


def test_unrelated_scope_alias_cannot_hide_outer_import():
    source = "import core.capabilities.plugins.archive as archive\narchive._extract()\ndef unrelated():\n    import pathlib as archive\n"
    assert contract.private_imports_in_module("core/services/market.py", source, ALIASES) == {"private-symbol: features.marketplace.source -> core.capabilities.plugins.archive._extract"}


def test_dependency_identity_survives_a_physical_move(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(contract, "canonical_names", lambda: {"core.old": "core.models.client"})
    monkeypatch.setattr(contract, "private_imports", lambda: set())
    def graph(source):
        return SimpleNamespace(modules=[source], find_modules_directly_imported_by=lambda _: ["core.llm.stub"])
    assert contract.violations(graph("core.old")) == contract.violations(graph("core.models.client")) == {"core-layers: core.models.client -> core.llm.stub"}
