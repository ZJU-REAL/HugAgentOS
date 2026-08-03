"""Evolution refuses to run without the dependencies it needs.

The state this exists to prevent: an embedding service is absent, evolution
still wakes up on schedule, every engine falls back to something much weaker,
the cycle reports success, and it finds nothing. That is indistinguishable from
"there was nothing to find" — so nobody investigates, and the feature looks
switched on while doing almost nothing.
"""

import pytest

from core.evolution import loop as L
from core.evolution import personal as PE
from core.evolution import readiness as R


def _not_ready(reason="embedding"):
    return R.Readiness(
        ready=False,
        requirements=[
            R.Requirement(R.REQUIREMENT_EMBEDDING, "向量模型（embedding）", False, reason),
            R.Requirement(R.REQUIREMENT_GENERATOR, "生成模型", True, "ok"),
        ],
    )


def test_the_fleet_cycle_refuses_rather_than_running_degraded(monkeypatch):
    monkeypatch.setattr(R, "check_readiness", lambda **_kw: _not_ready())
    report = L.run_evolution_cycle(limit=10)
    assert report["skipped"] == "not_ready"
    assert report["candidates"] == []
    # And it says which dependency, because "not ready" without a reason is the
    # same dead end as no message at all.
    assert report["readiness"]["blocking"] == ["embedding"]


def test_the_personal_cycle_refuses_too(monkeypatch):
    monkeypatch.setattr(R, "check_readiness", lambda **_kw: _not_ready())
    report = PE.run_personal_cycle("u1")
    assert report["skipped"] == "not_ready"
    assert report["candidates"] == []


def test_readiness_reports_each_dependency_separately():
    """They fail differently and are fixed by different people."""
    readiness = _not_ready()
    assert readiness.blocking == ["embedding"]
    keys = {r["key"] for r in readiness.to_dict()["requirements"]}
    assert keys == {"embedding", "generator"}


def test_an_unreachable_embedding_service_is_not_ready(monkeypatch):
    """Configured is not the same as answering.

    A configured-but-unreachable endpoint produces exactly the silent
    degradation this check exists to surface.
    """
    import core.kb.kb_vector as kb_vector

    def explode(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(kb_vector, "embed_text", explode)
    R.reset_cache()
    requirement = R._check_embedding()
    assert requirement.ok is False
    # The impact is stated in the operator's terms, not the code's.
    assert "抓不到" in requirement.impact


def test_an_empty_vector_is_not_ready(monkeypatch):
    import core.kb.kb_vector as kb_vector

    monkeypatch.setattr(kb_vector, "embed_text", lambda *_a, **_kw: [])
    R.reset_cache()
    assert R._check_embedding().ok is False


@pytest.mark.real_readiness
def test_the_probe_is_cached_so_a_cycle_does_not_re_probe(monkeypatch):
    """A personal cycle can run often; re-probing each time spends a request to
    re-learn something that was true seconds ago."""
    calls = []

    def counting_probe():
        calls.append(1)
        return R.Requirement(R.REQUIREMENT_EMBEDDING, "向量模型", True, "ok")

    monkeypatch.setattr(R, "_check_embedding", counting_probe)
    monkeypatch.setattr(
        R, "_check_generator", lambda: R.Requirement(R.REQUIREMENT_GENERATOR, "生成", True)
    )
    R.reset_cache()
    R.check_readiness()
    R.check_readiness()
    R.check_readiness()
    assert len(calls) == 1

    # …and a configuration fix is picked up without waiting for the TTL.
    R.reset_cache()
    R.check_readiness()
    assert len(calls) == 2
