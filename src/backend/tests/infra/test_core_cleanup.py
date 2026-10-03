"""Behavioral contracts for shared lifecycle operations."""

import pytest

from core.db.models import Artifact, UserShadow
from core.db.repository.artifact import ArtifactRepository


def test_owned_deletion_retains_other_users_files_and_is_idempotent(db_session):
    db_session.add_all(
        [
            UserShadow(user_id="cleanup-owner", username="owner"),
            UserShadow(user_id="cleanup-other", username="other"),
        ]
    )
    db_session.add(
        Artifact(
            artifact_id="cleanup-file",
            user_id="cleanup-owner",
            type="document",
            title="file.txt",
            filename="file.txt",
            storage_key="file.txt",
            size_bytes=1,
            mime_type="text/plain",
        )
    )
    db_session.commit()
    repo = ArtifactRepository(db_session)
    assert not repo.soft_delete_owned("cleanup-file", "cleanup-other")
    assert repo.get_by_id("cleanup-file") is not None
    assert repo.soft_delete_owned("cleanup-file", "cleanup-owner")
    assert repo.get_by_id("cleanup-file") is None
    assert not repo.soft_delete_owned("cleanup-file", "cleanup-owner")


def test_circuit_snapshot_reports_only_supplied_live_breakers():
    from core.infra.circuit_breaker import CircuitBreaker, snapshot_circuit_breakers

    breaker = CircuitBreaker("actual-store", failure_threshold=1, timeout=60)

    def fail():
        raise ValueError("store unavailable")

    with pytest.raises(ValueError):
        breaker.call(fail)
    rows = snapshot_circuit_breakers([breaker])
    assert len(rows) == 1
    assert rows[0]["name"] == "actual-store"
    assert rows[0]["state"] == "OPEN"
    assert rows[0]["failure_count"] == 1
    assert snapshot_circuit_breakers([]) == []


@pytest.mark.asyncio
async def test_decorated_async_failure_counts_toward_circuit():
    from core.infra.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

    breaker = CircuitBreaker("async-store", failure_threshold=1)

    @breaker
    async def fail():
        raise ValueError("store unavailable")

    with pytest.raises(ValueError):
        await fail()
    with pytest.raises(CircuitBreakerOpenError):
        await fail()


@pytest.mark.parametrize(
    "module", ["runtime.preparation", "runtime.connectors", "runtime.dependencies"]
)
def test_runtime_operation_modules_import_without_order_dependency(module):
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", f"import core.capabilities.{module}"],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_decorated_async_callable_counts_failure():
    from core.infra.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError

    class Store:
        async def __call__(self):
            raise ValueError("store unavailable")

    call = CircuitBreaker("callable-store", failure_threshold=1)(Store())
    with pytest.raises(ValueError):
        await call()
    with pytest.raises(CircuitBreakerOpenError):
        await call()
