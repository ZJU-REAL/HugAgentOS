"""Run the cycle's secondary engines in one call."""
from __future__ import annotations
from typing import Any, Dict, Optional, Sequence
import logging
from core.evolution.cycle import extra_findings
from core.evolution.cycle import memory_findings

logger = logging.getLogger(__name__)

def run_extras(
    views: Sequence[Dict[str, Any]],
    *,
    scan_findings: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Everything the cycle was missing, in one call.

    Each section is independently guarded: a failure in the decremental pass
    must not cost the intent pass its findings, because they answer different
    questions and an operator needs whichever ones succeeded.
    """
    report: Dict[str, Any] = {}
    for name, fn in (
        ("intent", lambda: extra_findings.intent_findings(views)),
        ("decremental", lambda: extra_findings.decremental_findings(views)),
        ("memory", lambda: memory_findings.memory_findings(views, scan_findings=scan_findings)),
    ):
        try:
            report[name] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[cycle-extras] %s pass failed: %s", name, exc)
            report[name] = {"error": str(exc)}
    return report
