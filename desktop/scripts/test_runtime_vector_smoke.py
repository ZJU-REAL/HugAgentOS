"""Exercise the shipped vector-store probe without touching user data."""

import json
import os
from pathlib import Path
import subprocess
import sys

SMOKE = Path(__file__).with_name("runtime-smoke.py")


def test_vector_probe_uses_temporary_database_and_reopens_persisted_rows(tmp_path):
    user_database = tmp_path / "existing-memory"
    user_database.mkdir()
    marker = user_database / "manifest.json"
    marker.write_bytes(b"existing-user-memory-must-stay-unchanged")
    result = subprocess.run(
        [sys.executable, str(SMOKE), "--vector-only"],
        env={**os.environ, "MILVUS_URL": str(user_database)},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["vector_store"] == {"persisted_rows": 2}
    assert marker.read_bytes() == b"existing-user-memory-must-stay-unchanged"
    assert sorted(p.name for p in user_database.iterdir()) == ["manifest.json"]
