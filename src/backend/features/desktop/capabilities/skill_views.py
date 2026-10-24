"""skill views responsibilities."""
from __future__ import annotations
from features.desktop.capabilities import skills
from pathlib import Path
from typing import Dict, Optional
from features.desktop.capabilities import registry
from features.desktop.capabilities import view as view_mod
from core.capabilities.identity_rules import KIND_SKILL
from features.desktop.capabilities.resolver import Resolution, resolve
import logging
logger = logging.getLogger(__name__)
from features.desktop.capabilities import skill_candidates
from features.desktop.capabilities import skill_paths
from features.desktop.capabilities import skills

def targets(res: Resolution) -> Dict[str, Path]:
    return {name: c.path for name, c in res.chosen.items() if c.path is not None}


def rebuild_device_view() -> view_mod.ViewReport:
    res = resolve(
        KIND_SKILL,
        skill_candidates.fill_hashes(skill_candidates.builtin_candidates() + skill_candidates.local_candidates(None, shared_only=True)),
        preferences=registry.preferences(KIND_SKILL),
    )
    report = view_mod.build_view(
        skill_paths.device_view_dir(), targets(res), allowed_roots=skill_paths.allowed_link_roots()
    )
    log_report("device", report)
    return report


def rebuild_user_view(user_id: str) -> Optional[view_mod.ViewReport]:
    vdir = skill_paths.user_view_dir(user_id)
    if vdir is None:
        return None
    res = skills.resolve_for_user(user_id)
    report = view_mod.build_view(vdir, targets(res), allowed_roots=skill_paths.allowed_link_roots())
    log_report(f"user:{user_id}", report)
    return report


def rebuild_views(user_id: Optional[str]) -> Dict[str, view_mod.ViewReport]:
    reports = {"device": rebuild_device_view()}
    if user_id:
        user_report = rebuild_user_view(user_id)
        if user_report is not None:
            reports["user"] = user_report
    return reports


def log_report(label: str, report: view_mod.ViewReport) -> None:
    if report.changed:
        logger.info(
            "[caps-view] %s linked=%d relinked=%d removed=%d",
            label,
            len(report.linked),
            len(report.relinked),
            len(report.removed),
        )
    for name, why in report.blocked.items():
        logger.warning("[caps-view] %s name '%s' unavailable: %s", label, name, why)
