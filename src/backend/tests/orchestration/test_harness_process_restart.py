"""Black-box proof that every Harness safe point survives an OS process death."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires POSIX SIGKILL")
def test_sigkill_restart_matrix_covers_every_durable_safe_point(tmp_path):
    backend_root = Path(__file__).resolve().parents[2]
    script = backend_root / "scripts" / "harness_fault_injection.py"
    state_dir = tmp_path / "fault-matrix"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "matrix",
            "--state-dir",
            str(state_dir),
            "--lease-seconds",
            "1",
            "--timeout",
            "45",
        ],
        cwd=backend_root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    payload = json.loads(completed.stdout.splitlines()[-1])
    assert payload["state"] == "matrix_passed"
    assert payload["signal"] == "SIGKILL"
    assert payload["killed_phases"] == [
        "pending",
        "model_before",
        "model_after",
        "tool_intent",
        "tool_unknown",
        "message_committed",
        "compacting",
        "memory_outbox",
    ]
