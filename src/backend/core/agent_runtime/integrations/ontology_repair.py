"""Answer repair only follows a claimed review, which never happens in CE."""

from __future__ import annotations

from typing import Any


async def _run_ontology_repair_round(*_args: Any, **_kwargs: Any):
    raise RuntimeError("Community edition has no ontology review to repair")
