"""Retirement evidence must survive the JSON database boundary."""

import json
import math

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models.evolution import EvolutionCandidate, EvolutionCreditDecision
from core.evolution.cycle import candidate_records, composition_candidates, retirement


@pytest.mark.parametrize("exposure", [float("inf"), 60.0])
@pytest.mark.parametrize(
    "stats",
    [
        {"offers": 40, "opens": 0, "success_rate": 0.0, "uplift": None},
        {"offers": 40, "opens": 20, "success_rate": 0.2, "uplift": None},
        {"offers": 40, "opens": 20, "success_rate": 0.9, "uplift": -0.2},
    ],
    ids=["never-opened", "low-success", "harmful"],
)
def test_retirement_candidate_persists_with_strict_json(monkeypatch, exposure, stats):
    # SQLite otherwise accepts Infinity and conceals the PostgreSQL failure.
    engine = create_engine(
        "sqlite:///:memory:",
        json_serializer=lambda value: json.dumps(value, allow_nan=False),
    )
    Base.metadata.create_all(
        engine,
        tables=[EvolutionCandidate.__table__, EvolutionCreditDecision.__table__],
    )
    factory = sessionmaker(bind=engine)
    import core.db.engine as engine_module

    monkeypatch.setattr(engine_module, "SessionLocal", factory)
    monkeypatch.setattr(candidate_records, "SessionLocal", factory)
    usage = {"builtin-skill": {**stats, "exposure_days": exposure, "follow_through": 0.9}}
    findings = retirement.find_retirement_candidates(usage)
    assert len(findings) == 1

    try:
        result = composition_candidates.decremental_candidates(
            {"decremental": {"retirements": findings}}, dry_run=False
        )
        assert len(result) == 1
        with factory() as db:
            row = db.get(EvolutionCandidate, result[0]["candidate_id"])
            assert row is not None
            stored = row.ir["changes"][0]["stats"]
            assert stored["exposure_days"] == (None if math.isinf(exposure) else exposure)
            assert stored["offers"] == stats["offers"]
            assert stored["action"] == findings[0]["action"]
            assert row.status == "draft"
            json.dumps(row.ir, allow_nan=False)
        # Serialisation must not change the evidence used for retirement decisions.
        assert findings[0]["exposure_days"] == exposure
        repeated = composition_candidates.decremental_candidates(
            {"decremental": {"retirements": findings}}, dry_run=False
        )
        assert repeated[0]["candidate_id"] == result[0]["candidate_id"]
    finally:
        engine.dispose()
