"""Test-wide defaults for the evolution suite.

Readiness probes a live embedding endpoint. A unit suite that makes network
calls is slow, order-dependent and fails wherever the network is absent — this
one already produced a test that passed alone and failed in the suite. The probe
is therefore stubbed by default; the tests that are *about* readiness override
it explicitly.
"""

import pytest

from core.evolution import readiness as R


@pytest.fixture(autouse=True)
def ready_by_default(request, monkeypatch):
    # Tests that are *about* the probe itself opt out, otherwise they would be
    # asserting against the stub rather than against the code.
    if request.node.get_closest_marker("real_readiness"):
        R.reset_cache()
        yield
        R.reset_cache()
        return
    monkeypatch.setattr(
        R,
        "check_readiness",
        lambda **_kw: R.Readiness(
            ready=True,
            requirements=[
                R.Requirement(R.REQUIREMENT_EMBEDDING, "向量模型（embedding）", True, "stub"),
                R.Requirement(R.REQUIREMENT_GENERATOR, "生成模型", True, "stub"),
            ],
        ),
    )
    R.reset_cache()
    yield
    R.reset_cache()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_readiness: exercise the real readiness probe instead of the stub",
    )
