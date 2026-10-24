"""CLI composes package providers before installing default or selected plugins."""
from unittest.mock import Mock


def test_cli_install_initializes_package_sources(monkeypatch):
    import cli
    from core.db import engine
    from core.capabilities.plugins import management
    from core.harness.registry import package_sources
    monkeypatch.setattr(package_sources.registration_state(), 'sources', {})
    monkeypatch.setattr(package_sources.registration_state(), 'upgrade_policy', None)
    db = Mock()
    monkeypatch.setattr(engine, "SessionLocal", lambda: db)
    seen = []
    def install(session, slug, **kwargs):
        assert session is db
        assert package_sources.package_source("marketplace") is not None
        assert package_sources.package_upgrade_policy() is not None
        seen.append(slug)
    monkeypatch.setattr(management, "install_plugin", install)
    assert cli.install_plugins(["automation", "sites"]) == ["automation", "sites"]
    assert seen == ["automation", "sites"]
    db.close.assert_called_once()
