"""Snapshot failures must release the transaction, including cancellation."""
import asyncio
from unittest.mock import Mock

import pytest

from core.chat import compaction_snapshots
from core.chat.service import ChatService


@pytest.mark.parametrize("failure", [RuntimeError("snapshot failed"), asyncio.CancelledError()])
def test_public_snapshot_rolls_back_every_failure(monkeypatch, failure):
    database = Mock()
    service = ChatService(database)

    def fail_snapshot(*args, **kwargs):
        raise failure

    monkeypatch.setattr(compaction_snapshots, "acquire_compaction_snapshot_impl", fail_snapshot)
    with pytest.raises(type(failure)) as caught:
        service.acquire_compaction_snapshot("chat", owner="worker", lease_seconds=30)
    assert caught.value is failure
    database.rollback.assert_called_once_with()
