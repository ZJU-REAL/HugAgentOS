"""Flat runtime view: ``<view_dir>/<runtime_name>`` → one stored revision each.

The view is a derived artifact — it can always be rebuilt from a resolution. Only
links are ever created or removed here; a real directory occupying a name is user
data, is never deleted, and blocks that name until the migration step imports it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

from . import junction
from .errors import ViewUnavailable


@dataclass
class ViewReport:
    view_dir: Path
    linked: List[str] = field(default_factory=list)
    relinked: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    blocked: Dict[str, str] = field(default_factory=dict)
    foreign: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.linked or self.relinked or self.removed)

    def to_dict(self) -> Dict[str, object]:
        return {
            "view_dir": str(self.view_dir),
            "linked": list(self.linked),
            "relinked": list(self.relinked),
            "removed": list(self.removed),
            "blocked": dict(self.blocked),
            "foreign": list(self.foreign),
        }


def build_view(
    view_dir: Path,
    targets: Dict[str, Path],
    *,
    allowed_roots: Iterable[Path],
) -> ViewReport:
    """Make ``view_dir`` contain exactly one link per name in ``targets``."""
    roots = list(allowed_roots)
    report = ViewReport(view_dir=view_dir)
    try:
        view_dir.mkdir(parents=True, exist_ok=True)
        existing = {p.name: p for p in view_dir.iterdir()}
    except OSError as exc:
        raise ViewUnavailable(
            "cannot access runtime view directory",
            details={"view_dir": str(view_dir), "os_error": str(exc)},
        ) from exc

    for name, entry in existing.items():
        try:
            if junction.is_directory_link(entry):
                if name not in targets:
                    junction.remove_directory_link(entry)
                    report.removed.append(name)
                continue
            if entry.is_dir():
                if name in targets:
                    report.blocked[name] = f"real directory occupies {entry}"
                else:
                    report.foreign.append(name)
            # Stray files are left alone; they are not ours.
        except (junction.LinkError, OSError) as exc:
            # A busy junction remains linked to its old target. It must also
            # block cleanup of an unselected name, or a runtime could use it.
            report.blocked[name] = str(exc)

    for name, target in sorted(targets.items()):
        if name in report.blocked:
            continue
        link = view_dir / name
        try:
            was_link = junction.is_directory_link(link)
            changed = junction.ensure_directory_link(link, target, allowed_roots=roots)
        except (junction.LinkError, OSError) as exc:
            report.blocked[name] = str(exc)
            continue
        if changed:
            (report.relinked if was_link else report.linked).append(name)
    return report


def link_targets(view_dir: Path) -> Dict[str, Path]:
    """Current name → target map of a view (links only)."""
    result: Dict[str, Path] = {}
    if not view_dir.is_dir():
        return result
    for entry in view_dir.iterdir():
        target = junction.read_directory_link(entry)
        if target is not None:
            result[entry.name] = target
    return result
