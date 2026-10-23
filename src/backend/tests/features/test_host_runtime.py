"""Remote execution policy remains dynamic and host composed."""
import subprocess
import sys


def test_core_policy_does_not_import_product_modules():
    subprocess.run([sys.executable, "-c", "from core.harness.registry.host_runtime import requires_remote_execution; import sys; assert not requires_remote_execution(); assert not any(n.startswith('features.') for n in sys.modules)"], check=True)


def test_host_policy_reads_current_bridge_setting(monkeypatch):
    from features import register_run_capabilities
    from features.desktop.auth import bridge as desktop_bridge
    from core.harness.registry.host_runtime import requires_remote_execution
    register_run_capabilities()
    monkeypatch.setattr(desktop_bridge, "bridge_enabled", lambda: True)
    assert requires_remote_execution()
    monkeypatch.setattr(desktop_bridge, "bridge_enabled", lambda: False)
    assert not requires_remote_execution()
