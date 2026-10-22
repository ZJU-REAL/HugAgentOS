"""Visualize installs as a shared default and exports usable offline previews."""

import subprocess
import sys
from pathlib import Path

import pytest
from core.db.models import AdminSkill, ContentBlock, InstalledPlugin
from core.capabilities.plugins import management as plugins
from core.capabilities.plugins.management.defaults import ensure_visualize_plugin_bootstrapped
from core.capabilities.plugins.packaging.importer import normalize_plugin_dir

BUNDLE = Path(__file__).resolve().parents[1] / "extensions/plugins/default/visualize"
SKILL = BUNDLE / "skills/visualize"


def test_existing_database_gets_visualize_without_restoring_removed_defaults(db_session):
    db_session.add(
        ContentBlock(
            id=plugins.DEFAULT_BOOTSTRAP_MARKER_ID,
            payload={"version": 1, "plugins": ["automation", "skill-manager", "sites"]},
        )
    )
    db_session.commit()
    assert ensure_visualize_plugin_bootstrapped(db_session)
    installed = plugins.list_installed(db_session, owner_user_id="user", include_global=True)
    assert [item["slug"] for item in installed] == ["visualize"]
    row = db_session.get(InstalledPlugin, "visualize@global")
    assert row.component_ids["mcp"] == []
    skills = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "visualize").all()
    assert len(skills) == 1 and skills[0].is_enabled
    plugins.uninstall_plugin(db_session, "visualize@global", owner_user_id=None)
    assert not ensure_visualize_plugin_bootstrapped(db_session)
    assert plugins.list_installed(db_session, owner_user_id="user", include_global=True) == []


def test_existing_visualize_disabled_state_is_preserved(db_session):
    plugins.install_plugin(db_session, "visualize", owner_user_id=None)
    skill = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "visualize").one()
    skill.is_enabled = False
    db_session.commit()
    assert ensure_visualize_plugin_bootstrapped(db_session)
    db_session.refresh(skill)
    assert skill.is_enabled is False


def test_native_bundle_includes_offline_resources():
    plugin = normalize_plugin_dir(BUNDLE)
    assert plugin.slug == "visualize" and len(plugin.skills) == 1
    assert plugin.mcp == [] and plugin.required_secrets == []
    assert plugin.default_enabled["skills"] == ["visualize"]
    assert "assets/vendor/d3-7.9.0.min.js" in plugin.skills[0].extra_files
    assert "scripts/render.py" in plugin.skills[0].extra_files
    text = plugin.skills[0].skill_content
    assert "pin_to_workspace" in text and "sandbox_get_artifact" in text
    for path in SKILL.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
            assert "zcode" not in path.read_text().lower(), path


def test_renderer_exports_bundled_chart_and_calendar_offline(tmp_path):
    fragment = tmp_path / "chart.fragment.html"
    fragment.write_text(
        '<div id="chart"><viz-calendar></viz-calendar></div>'
        '<script src="https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"></script>'
        '<script>document.getElementById("chart").dataset.value=d3.sum([2,3]);</script>'
    )
    output = tmp_path / "chart.html"
    subprocess.run(
        [sys.executable, str(SKILL / "scripts/render.py"), str(fragment), str(output)], check=True
    )
    html = output.read_text()
    assert 'sandbox="allow-scripts"' in html
    assert "data:text/javascript;base64," in html
    assert "customElements.define" in html
    assert 'src="https://' not in html
    assert "hugagent-visualization" in html
    result = subprocess.run(
        [sys.executable, str(SKILL / "scripts/render.py"), str(fragment), str(output)],
        capture_output=True,
    )
    assert result.returncode != 0  # Do not overwrite an existing output silently.


@pytest.mark.parametrize("local,edition", [(False, "ce"), (True, "ce"), (False, "ee")])
@pytest.mark.asyncio
async def test_startup_includes_visualize_in_all_profiles(monkeypatch, local, edition):
    from types import SimpleNamespace

    from api.startup import plugins as startup
    from core.db import engine
    from orchestration.workers import edition_startup

    monkeypatch.setenv("HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS", "1")
    monkeypatch.setattr('features.desktop.capabilities.device_catalog.active', lambda: False)
    calls = []
    session = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        startup,
        "settings",
        SimpleNamespace(
            edition=SimpleNamespace(edition=edition),
            deploy=SimpleNamespace(is_local=local),
        ),
    )
    monkeypatch.setattr(engine, "SessionLocal", lambda: session)
    monkeypatch.setattr(plugins, "ensure_default_plugins_bootstrapped", lambda db: False)
    monkeypatch.setattr(edition_startup, "bootstrap_edition_plugins", lambda db: ())
    monkeypatch.setattr(
        'features.workbench.ui.contributions.refresh_builtin_ui_contributions', lambda db: 0
    )
    monkeypatch.setattr(
        'core.capabilities.plugins.management.defaults.ensure_visualize_plugin_bootstrapped',
        lambda db: calls.append(db),
    )
    await startup._startup_seed_default_plugins()
    assert calls == [session]


@pytest.mark.parametrize("opt_in,hybrid", [(False, False), (True, True)])
@pytest.mark.asyncio
async def test_local_bootstrap_respects_opt_out_and_hybrid(monkeypatch, opt_in, hybrid):
    from types import SimpleNamespace

    from api.startup import plugins as startup
    from core.db import engine

    monkeypatch.setattr(
        startup,
        "settings",
        SimpleNamespace(
            edition=SimpleNamespace(edition="ce"),
            deploy=SimpleNamespace(is_local=True),
        ),
    )
    monkeypatch.setenv("HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS", "1" if opt_in else "0")
    monkeypatch.setattr('features.desktop.capabilities.device_catalog.active', lambda: hybrid)

    def unexpected_session():
        pytest.fail("Local opt-out and hybrid mode must not bootstrap global plugins")

    monkeypatch.setattr(engine, "SessionLocal", unexpected_session)
    await startup._startup_seed_default_plugins()


@pytest.mark.parametrize(
    "markup",
    [
        '<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>',
        '<img src="https://example.com/chart.png">',
        '<link rel="stylesheet" href="theme.css">',
        '<style>@import "https://fonts.googleapis.com/css2?family=Roboto";</style>',
    ],
)
def test_renderer_rejects_unbundled_resources(tmp_path, markup):
    fragment = tmp_path / "external.fragment.html"
    fragment.write_text(markup)
    output = tmp_path / "external.html"
    result = subprocess.run(
        [sys.executable, str(SKILL / "scripts/render.py"), str(fragment), str(output)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "embedded before export" in result.stderr
    assert not output.exists()
