"""Route modules import on their own, without composing the application first."""
from pathlib import Path
import importlib.util
import os
import subprocess
import sys

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _run(code, tmp_path):
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR",
                           "DATABASE_URL", "SQLITE_FALLBACK_URL"}
    }
    env.update(PYTHONPATH=str(BACKEND), HOME=str(tmp_path), PYTHONUTF8="1", REDIS_URL="")
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        timeout=120,
    )


@pytest.mark.parametrize(
    "entry",
    [
        # Each of these once imported ``api`` (and with it the whole app),
        # which re-imported the route module while it was half initialised.
        "edition_ee.api.deps",
        "edition_ee.api.openapi_data_schemas",
        "edition_ee.routes.config_teams",
        "edition_ee.routes.team_file_assets",
        "features.marketplace.api.admin_marketplace",
        "features.workbench.api.workbench_auth",
        "features.datasources.api.data_sources",
        # api.routes eagerly imported files -> local_file_actions -> api.routes.
        "features.desktop.api.local_file_actions",
        # device_catalog re-exports from device_plugin_catalog at import time.
        "features.desktop.capabilities.device_plugin_catalog",
    ],
)
def test_route_module_imports_first(entry, tmp_path):
    if importlib.util.find_spec(entry.split(".")[0]) is None:
        pytest.skip(f"{entry} is not part of this edition")
    result = _run(f"import {entry}\nprint('ROUTE_IMPORT_OK')", tmp_path)
    assert result.returncode == 0, result.stdout
    assert "ROUTE_IMPORT_OK" in result.stdout


def test_api_submodules_do_not_compose_the_app_but_app_still_resolves(tmp_path):
    code = """
import sys
import api.deps
from api.routes import file_location
assert "api.app" not in sys.modules
from api import app
from api.routes import files_router
from fastapi import FastAPI
assert isinstance(app, FastAPI)
from features.desktop.capabilities import device_catalog, device_plugin_catalog
assert device_catalog.plugin_entries is device_plugin_catalog.plugin_entries
print("API_LAZY_OK")
"""
    result = _run(code, tmp_path)
    assert result.returncode == 0, result.stdout
    assert "API_LAZY_OK" in result.stdout
