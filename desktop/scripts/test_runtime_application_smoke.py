"""Regression tests for the public source-only runtime smoke command."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SMOKE = Path(__file__).with_name("runtime-smoke.py")


class ApplicationSmokeTests(unittest.TestCase):
    def probe(self, app):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            backend = source / "src" / "backend"
            (backend / "api").mkdir(parents=True)
            (backend / "api" / "__init__.py").write_text("")
            (backend / "cli.py").write_text("def apply_local_env(port): pass")
            (backend / "api" / "app.py").write_text(app)
            return subprocess.run(
                [sys.executable, str(SMOKE), "--source-only", "--source", str(source)],
                env={**os.environ, "DATABASE_URL": "must-not-leak"},
                capture_output=True, text=True, timeout=15)

    def test_deferred_router_import_failure_blocks_build(self):
        result = self.probe("import missing_desktop_router_dependency")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing_desktop_router_dependency", result.stderr)

    def test_application_uses_isolated_environment(self):
        result = self.probe(
            "import os\n"
            "assert 'DATABASE_URL' not in os.environ\n"
            "assert os.environ['HOME'] == os.environ['USERPROFILE']\n"
            "assert os.environ['HUGAGENT_HOME'].startswith(os.environ['HOME'])\n"
            "app = object()\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["modules"], ["cli", "api.app"])


if __name__ == "__main__":
    unittest.main()
