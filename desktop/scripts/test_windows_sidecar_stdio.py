"""Verify sidecar logging handles beneath a GUI-style Windows parent."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt", "requires native Windows process handles")
class WindowsSidecarStdioTests(unittest.TestCase):
    def test_hidden_parent_provides_logging_streams_to_a_sidecar(self):
        backend = Path(os.environ.get(
            "SIDECAR_TEST_BACKEND",
            str(Path(__file__).resolve().parents[2] / "src" / "backend"),
        )).resolve()
        parent = """
import asyncio, sys
from core.infra.local_runtime import lifecycle, startup
child = (
    "import sys; assert sys.stdout is not None; assert sys.stderr is not None; "
    "import uvicorn; from fastapi import FastAPI; uvicorn.Config(FastAPI()); "
    "print('SIDECAR_LOG_READY', flush=True)"
)
async def probe():
    proc = await lifecycle._create("stdio_probe", [sys.executable, "-c", child])
    assert proc is not None
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=10)
        assert code == 0, code
    finally:
        if proc.returncode is None:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        startup._PROCS.clear()
        startup._SPECS.clear()
asyncio.run(probe())
"""
        with tempfile.TemporaryDirectory(prefix="sidecar-stdio-") as folder:
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP")}
            env.update(PYTHONPATH=str(backend), HOME=folder, USERPROFILE=folder,
                       DATABASE_URL="sqlite://", AUTH_MODE="mock",
                       SESSION_STORE="memory", JX_EDITION="ce",
                       PYTHONIOENCODING="utf-8")
            log_path = Path(folder) / "parent.log"
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-c", parent], cwd=folder, env=env,
                    stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    code = process.wait(timeout=20)
                finally:
                    if process.poll() is None:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        process.wait(timeout=5)
            output = log_path.read_text(encoding="utf-8", errors="replace")
            self.assertEqual(code, 0, output)
            self.assertIn("SIDECAR_LOG_READY", output)


if __name__ == "__main__":
    unittest.main()
