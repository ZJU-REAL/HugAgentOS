"""Local schema composition preserves provisioning and startup failure semantics."""
import os
from pathlib import Path
import subprocess
import sys


def run_script(tmp_path, code):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[3]),
               DEPLOY_PROFILE="local", JX_EDITION="ce", ENV="dev",
               DATABASE_URL="sqlite:///" + str(tmp_path / "platform.sqlite"),
               STORAGE_PATH=str(tmp_path / "storage"), APPLICATION_DATABASE_URL="",
               HUGAGENT_DESKTOP_BRIDGE_SECRET="", PERSONAL_AGENT_ENABLED="false")
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr[-4000:]


def test_schema_only_composition_provisions_without_api_or_workers(tmp_path):
    run_script(tmp_path, """
import sys
from features.installed import INSTALLED_FEATURES
from core.harness.registry import load_models
from core.harness.registry.schema_lifecycle import load_schema_lifecycle
from core.db.engine import init_db, engine
from features.hosted_apps.store import application_engine, require_store
load_models(INSTALLED_FEATURES)
load_schema_lifecycle(INSTALLED_FEATURES)
load_schema_lifecycle(INSTALLED_FEATURES)
assert 'api.app' not in sys.modules
init_db()
require_store(application_engine())
assert application_engine().url.database != engine.url.database
init_db()
require_store(application_engine())
assert 'api.app' not in sys.modules
""")


def test_schema_callback_replacement_order_and_failures(tmp_path):
    run_script(tmp_path, """
from core.harness.registry.schema_lifecycle import register_schema_lifecycle, initialize_local_schema
seen=[]
register_schema_lifecycle('first', lambda: seen.append('old'))
register_schema_lifecycle('second', lambda: seen.append('second'))
register_schema_lifecycle('first', lambda: seen.append('first'))
initialize_local_schema()
assert seen == ['first','second']
def fail():
    raise RuntimeError('provisioning failed')
register_schema_lifecycle('first', fail)
seen.clear()
try:
    initialize_local_schema()
except RuntimeError as error:
    assert str(error) == 'provisioning failed'
else:
    raise AssertionError('startup failure was swallowed')
assert seen == []
""")
