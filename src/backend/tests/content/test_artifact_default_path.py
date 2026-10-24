"""Relocating artifact modules must not relocate an existing local store."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("storage_path", [None, "", "configured"])
def test_artifact_store_root_survives_module_relocation(tmp_path, storage_path):
    backend = Path(__file__).resolve().parents[2]
    expected = backend.parent / "result" / "artifacts"
    env = dict(os.environ, PYTHONPATH=str(backend))
    env.pop("STORAGE_PATH", None)
    if storage_path is not None:
        env["STORAGE_PATH"] = "" if not storage_path else str(tmp_path / storage_path)
    if storage_path:
        expected = tmp_path / storage_path / "artifacts"
    result = subprocess.run(
        [sys.executable, "-c", "from core.files.artifacts.store import _STORE_DIR; print(_STORE_DIR)"],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    assert Path(result.stdout.strip()) == expected.resolve()
