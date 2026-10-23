"""Installed schemas survive disabled runtime features and headless startup."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace


def test_models_are_registered_before_disabled_setup(monkeypatch):
    import core.harness.registry as registry
    import core.harness.registry.edition as edition
    import core.harness.registry.schema_lifecycle as schema_lifecycle

    calls = []

    def imported(name):
        calls.append(name)
        if name.endswith(".extension"):
            return SimpleNamespace(NAME="example", setup=lambda: False)
        return SimpleNamespace()

    monkeypatch.setattr(registry, "import_module", imported)
    monkeypatch.setattr(schema_lifecycle, "import_module", imported)
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(edition, "EDITION_EXTENSIONS", ())
    monkeypatch.setattr(registry, "mark_enabled", lambda name: calls.append("enabled:" + name))
    registry.load_extensions(("features.example",))
    registry.load_extensions(("features.example",))
    assert calls == [
        "features.example.models",
        "features.example.extension",  # Schema hooks load even when setup is disabled.
        "features.example.extension",
    ]


def test_model_loading_does_not_call_extension_setup(monkeypatch):
    import core.harness.registry as registry

    imports = []
    monkeypatch.setattr(registry, "import_module", imports.append)
    registry.load_models(("features.first", "features.second"))
    assert imports == ["features.first.models", "features.second.models"]


def test_disabled_personal_agent_schema_available_without_api():
    env = dict(os.environ, PERSONAL_AGENT_ENABLED="false")
    backend = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(backend)
    script = """
import sys
from core.db import models
from core.db.engine import Base
from core.harness.registry import load_models, is_enabled
from features.installed import INSTALLED_FEATURES
load_models(INSTALLED_FEATURES)
assert 'personal_agents' in Base.metadata.tables
assert 'personal_devices' in Base.metadata.tables
assert 'personal_agent.extension' not in sys.modules
assert 'api.app' not in sys.modules
assert not is_enabled('personal_agent')
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
