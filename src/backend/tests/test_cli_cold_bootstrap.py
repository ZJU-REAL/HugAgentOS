"""Exercise CLI plugin bootstrap in a fresh process before API composition."""
from pathlib import Path
import os
import subprocess
import sys


def test_cli_default_plugins_bootstrap_before_api_import(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR"}
    }
    env.update(
        PYTHONPATH=str(backend),
        HOME=str(tmp_path),
        USERPROFILE=str(tmp_path),
        HUGAGENT_CAPS_ROOT=str(tmp_path / "caps"),
        PYTHONUTF8="1",
        JX_EDITION="ce",
        REDIS_URL="",
    )
    code = """
import os
import sys
import cli
os.environ[cli._DEFAULT_PLUGIN_BOOTSTRAP_ENV] = "1"
cli.apply_local_env(33298)
from hugagent_cli.schema import ensure_schema_and_seed
ensure_schema_and_seed()
assert "api.app" not in sys.modules
cli.bootstrap_default_plugins_if_needed()
assert cli._default_plugins_marker().is_file()
assert cli.ensure_default_plugins_once() is False
assert "api.app" not in sys.modules
print("CLI_COLD_BOOTSTRAP_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        timeout=90,
    )
    assert result.returncode == 0, result.stdout
    assert "CLI_COLD_BOOTSTRAP_OK" in result.stdout
