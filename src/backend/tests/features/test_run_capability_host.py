"""Independent processes must explicitly install the same runtime selection as API/CLI."""
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("enabled", [False, True])
def test_headless_capability_host_preserves_dynamic_selection(enabled, tmp_path):
    env = dict(os.environ)
    env["HUGAGENT_CAPS_ROOT"] = str(tmp_path) if enabled else ""
    script = 'import asyncio\nimport os\nimport sys\nfrom features import register_run_capabilities\nfrom core.harness.registry.run_capabilities import has_run_capabilities, run_capability_provider, prepare_run_capabilities\nfrom features.desktop.capabilities.paths import capabilities_enabled\n\nregister_run_capabilities()\nregister_run_capabilities()\nassert has_run_capabilities() == capabilities_enabled()\nassert "api.app" not in sys.modules\nos.environ["HUGAGENT_CAPS_ROOT"] = "/tmp/core-refactor-headless-proof"\nassert has_run_capabilities()\nassert type(run_capability_provider()).__name__ == "DesktopRunCapabilityProvider"\nos.environ.pop("HUGAGENT_CAPS_ROOT")\nassert not has_run_capabilities()\nassert asyncio.run(prepare_run_capabilities(None, _caps_skill_ids=["selected"])) == (["selected"], None, None)\nassert "api.app" not in sys.modules\nprint("headless capability selection passed: desktop -> cloud, no API import")\n'
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
