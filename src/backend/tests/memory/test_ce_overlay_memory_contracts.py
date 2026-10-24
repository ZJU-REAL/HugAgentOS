"""Contract regressions for the CE overlay's memory-related files.

Two bugs shipped in the 0.2.15 desktop local build:
1. The CE audit stub narrowed its signature to ``(ctx=None, **kwargs)`` while
   callers use the real implementation's positional style
   ``record_sync(ctx, action, layer, ...)`` — the TypeError fired *after* the
   business transaction committed, reporting a successful L1 write as failed.
2. The CE models facade skipped the evolution models and MemoryRefShadow, so
   they never entered Base.metadata and the local SQLite bootstrap could not
   create their tables ("no such table" at runtime).

The shared implementations now use a small CE audit policy. These tests load
that policy, exercise the real APIs and verify the shared models enter CE
metadata; they are skipped where the source overlay is absent.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_OVERLAY = Path(__file__).resolve().parents[4] / "ce" / "overlay" / "src" / "backend"

pytestmark = pytest.mark.skipif(
    not _OVERLAY.is_dir(), reason="ce/overlay is absent (derived CE tree)"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_ce_audit_stub_accepts_positional_calls_like_the_real_impl(ce_audit):
    stub = ce_audit
    ctx = SimpleNamespace(user_id="u1", workspace_id=None)

    # Same positional call style as core/memory/profile.py::_audit_sync_safe.
    stub.record_sync(ctx, "write", "L1", content="v", reason=None)
    stub.record_sync(ctx, "update", "L1", content="v")


@pytest.mark.asyncio
async def test_ce_audit_stub_async_variants_accept_positional_calls(ce_audit):
    stub = ce_audit
    ctx = SimpleNamespace(user_id="u1", workspace_id=None)

    await stub.record(ctx, "write", "L1", reason="r")
    await stub.record_batch(ctx, [{"action": "write", "layer": "L2"}])


def test_ce_models_facade_exports_evolution_and_ref_shadow_tables():
    from core.db import models
    from core.db.personal_schema import ce_metadata
    metadata = ce_metadata()
    required = [
        "EvolutionAgentProfile",
        "EvolutionCandidate",
        "EvolutionCreditDecision",
        "EvolutionEpisode",
        "EvolutionEvaluation",
        "EvolutionEvidencePack",
        "EvolutionMemoryOp",
        "EvolutionPromotionLink",
        "EvolutionRelease",
        "EvolutionTraceEvent",
        "MemoryRefShadow",
    ]
    missing = [name for name in required if not hasattr(models, name)]
    # A model missing from the facade never enters Base.metadata, so the local
    # bootstrap (create_all / reconcile) cannot create its table.
    assert not missing, f"CE models facade is missing imports: {missing}"

    for name in required:
        assert getattr(models, name).__tablename__ in metadata.tables


@pytest.fixture
def ce_audit(monkeypatch):
    from core.memory import audit, edition
    policy = _load(_OVERLAY / "core/memory/edition.py", "_ce_memory_policy")
    from unittest.mock import Mock
    assert policy.audit_enabled() is False
    enabled = Mock(wraps=policy.audit_enabled)
    monkeypatch.setattr(edition, "audit_enabled", enabled)
    monkeypatch.setattr(audit, "settings", SimpleNamespace(memory=SimpleNamespace(enabled=True)))
    single, batch = Mock(), Mock()
    monkeypatch.setattr(audit, "_sync_insert_one", single)
    monkeypatch.setattr(audit, "_sync_insert_many", batch)
    yield audit
    assert enabled.call_count == 2
    single.assert_not_called()
    batch.assert_not_called()
