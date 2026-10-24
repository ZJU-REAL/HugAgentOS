"""Execution identity must fail closed until the host binds its implementation."""
import os
import subprocess
import sys


def test_headless_identity_does_not_import_desktop_or_authorize():
    script = """
import sys
from core.harness.registry import execution_identity, execution_account, execution_catalog
assert not any(name.startswith('features.desktop') for name in sys.modules)
assert execution_identity.get_state() is None
assert execution_identity.state_fingerprint(None) == ''
assert execution_account.current_local_user_id() is None
assert not execution_account.account_authorized_for('user-1')
assert execution_catalog.apply_to_enabled_skill_ids(['s']) == ['s']
assert execution_catalog.cloud_gateway_mcp_configs() == {}
for operation in (
    lambda: execution_identity.require_current_account({'token': 'test'}),
    lambda: execution_identity.cloud_headers({'token': 'test'}),
    lambda: execution_identity.ensure_current_authorization(),
):
    try:
        operation()
    except RuntimeError:
        pass
    else:
        raise AssertionError('unregistered identity authorized an operation')
"""
    result = subprocess.run([sys.executable, '-c', script], env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_headless_tool_host_registers_marketplace_without_api():
    script = """
import sys
from features import register_product_tools
from core.harness.registry.marketplace_catalog import marketplace_catalog
register_product_tools()
assert type(marketplace_catalog()).__name__ == 'MarketplaceCatalogProvider'
assert 'api.app' not in sys.modules
"""
    result = subprocess.run([sys.executable, '-c', script], env=os.environ.copy(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
