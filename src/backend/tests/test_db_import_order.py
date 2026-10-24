"""Database models load no matter which module a fresh process imports first."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

BACKEND = Path(__file__).resolve().parents[1]

CHECK = """
import core.db as db
import core.db.models as models
from core.db import ChatSession, Team, get_db
from core.db.models import *  # noqa: F403
from core.db.models import ProjectMember, Team as ModelTeam
assert ModelTeam is Team and ProjectMember is models.ProjectMember
assert {"Team", "ChatSessionUserState"} <= set(models.__all__)
registry = db.Base.registry._class_registry
assert {"Team", "ProjectMember", "ChatSession"} <= set(registry)
print("DB_IMPORT_ORDER_OK")
"""


@pytest.mark.parametrize(
    "entry",
    [
        "edition_ee.db.model_extensions",
        "edition_ee.db.models",
        "edition_ee.db.models.feedback",
        "core.db.utc_datetime",
        "core.auth.local_users",
        "core.db.models",
    ],
)
def test_models_import_from_any_entry_point(entry, tmp_path):
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR"}
    }
    env.update(PYTHONPATH=str(BACKEND), HOME=str(tmp_path), PYTHONUTF8="1", REDIS_URL="")
    result = subprocess.run(
        [sys.executable, "-c", f"import {entry}\n{CHECK}"],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stdout
    assert "DB_IMPORT_ORDER_OK" in result.stdout


def test_db_leaf_modules_do_not_load_models(tmp_path):
    code = "import sys, core.db.engine, core.db.utc_datetime\nassert 'core.db.models' not in sys.modules\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(BACKEND)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "entry",
    [
        "features.automation.models",
        "features.automation.service",
        "extensions.mcp_servers.automation_task_mcp.server",
    ],
)
def test_automation_relationships_resolve_in_a_cold_process(entry, tmp_path):
    code = f"""
import {entry}
from features.automation.models import ScheduledTask
from sqlalchemy.orm import configure_mappers
configure_mappers()
assert ScheduledTask.user.property.mapper.class_.__name__ == "UserShadow"
assert ScheduledTask.plan.property.mapper.class_.__name__ == "Plan"
print("AUTOMATION_MODELS_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(BACKEND),
            "DATABASE_URL": f"sqlite:///{tmp_path / 'automation.db'}",
            "SQLITE_FALLBACK_URL": f"sqlite:///{tmp_path / 'fallback.db'}",
            "REDIS_URL": "",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout
    assert "AUTOMATION_MODELS_OK" in result.stdout
