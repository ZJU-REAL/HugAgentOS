"""Atomic shared Redis budgets and fail-closed behavior."""
import asyncio
import shutil
import socket
import subprocess
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from redis.asyncio import Redis
from core.services import site_rate_limit as limiter

@pytest.mark.asyncio
async def test_two_workers_share_atomic_budget(tmp_path, monkeypatch):
    executable = shutil.which("redis-server")
    if not executable:
        pytest.skip("Disposable Redis binary unavailable")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen([executable, "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no", "--dir", str(tmp_path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    clients = [Redis(host="127.0.0.1", port=port) for _ in range(2)]
    try:
        for _ in range(50):
            try:
                await clients[0].ping()
                break
            except OSError:
                await asyncio.sleep(0.02)
            except Exception:
                await asyncio.sleep(0.02)
        monkeypatch.setattr(limiter, "redis_configured", lambda: True)
        index = 0
        def select_client():
            nonlocal index
            index += 1
            return clients[index % 2]
        monkeypatch.setattr(limiter, "get_redis", select_client)
        results = await asyncio.gather(*(limiter.count_attempt("probe", "site") for _ in range(80)), return_exceptions=True)
        assert sum(item is None for item in results) == 60
        assert all(isinstance(item, HTTPException) and item.status_code == 429 for item in results if item is not None)
        monkeypatch.setattr(limiter, "redis_configured", lambda: False)
        monkeypatch.setattr(limiter, "settings", SimpleNamespace(deploy=SimpleNamespace(is_local=False)))
        with pytest.raises(HTTPException) as error:
            await limiter.count_attempt("probe", "site")
        assert error.value.status_code == 503
    finally:
        await asyncio.gather(*(client.aclose() for client in clients))
        process.terminate()
        process.wait(timeout=5)
