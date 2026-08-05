"""Contract regressions for the CE overlay's memory-related files.

Two bugs shipped in the 0.2.15 desktop local build:
1. The CE audit stub narrowed its signature to ``(ctx=None, **kwargs)`` while
   callers use the real implementation's positional style
   ``record_sync(ctx, action, layer, ...)`` — the TypeError fired *after* the
   business transaction committed, reporting a successful L1 write as failed.
2. The CE models facade skipped the evolution models and MemoryRefShadow, so
   they never entered Base.metadata and the local SQLite bootstrap could not
   create their tables ("no such table" at runtime).

Both files live under ce/overlay/ (they replace same-named files when the CE
tree is derived); these tests load them by path from the FULL tree and are
skipped in a derived CE tree where ce/ does not exist.
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


def test_ce_audit_stub_accepts_positional_calls_like_the_real_impl():
    stub = _load(_OVERLAY / "core" / "memory" / "audit.py", "_ce_audit_stub")
    ctx = SimpleNamespace(user_id="u1", workspace_id=None)

    # Same positional call style as core/memory/profile.py::_audit_sync_safe.
    stub.record_sync(ctx, "write", "L1", content="v", reason=None)
    stub.record_sync(ctx, "update", "L1", content="v")


@pytest.mark.asyncio
async def test_ce_audit_stub_async_variants_accept_positional_calls():
    stub = _load(_OVERLAY / "core" / "memory" / "audit.py", "_ce_audit_stub_async")
    ctx = SimpleNamespace(user_id="u1", workspace_id=None)

    await stub.record(ctx, "write", "L1", reason="r")
    await stub.record_batch(ctx, [{"action": "write", "layer": "L2"}])


def test_ce_models_facade_exports_evolution_and_ref_shadow_tables():
    src = (_OVERLAY / "core" / "db" / "models" / "__init__.py").read_text(encoding="utf-8")
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
    missing = [name for name in required if name not in src]
    # A model missing from the facade never enters Base.metadata, so the local
    # bootstrap (create_all / reconcile) cannot create its table.
    assert not missing, f"CE models facade is missing imports: {missing}"
