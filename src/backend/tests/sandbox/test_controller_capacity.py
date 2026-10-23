"""Capacity includes all controller inventory and competing creation processes."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
import pytest

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("sandbox_capacity", ROOT / "docker/opensandbox_capacity.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_inventory_and_atomic_creation_share_one_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "Path", lambda _: tmp_path)
    monkeypatch.setenv("OPENSANDBOX_POOL_MAX_TOTAL", "1")
    inventory = []
    first = SimpleNamespace(network_mode="compute", _owned_sandboxes=lambda: inventory)
    second = SimpleNamespace(network_mode="compute", _owned_sandboxes=lambda: inventory)
    def compete():
        with pytest.raises(HTTPException) as caught:
            with module.sandbox_capacity(second):
                pytest.fail("second creator admitted while first is pending")
        return caught.value.status_code
    with module.sandbox_capacity(first):
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(compete).result(timeout=1) == 429
        inventory.append("sandbox")
    # A restarted provider/controller sees inventory, without local reservations.
    with pytest.raises(HTTPException) as caught:
        with module.sandbox_capacity(second):
            pytest.fail("capacity exceeded after first creation")
    assert caught.value.status_code == 429
    inventory.clear()
    with module.sandbox_capacity(second):
        pass
