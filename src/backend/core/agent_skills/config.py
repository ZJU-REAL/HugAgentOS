"""Configuration for multi-source skill loading."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillSourceConfig:
    """Configuration for a skill source."""

    name: str  # Human-readable name (e.g., "built-in", "user", "project")
    root_dir: Path  # Root directory containing skill folders
    priority: int  # Priority for conflict resolution (higher = higher priority)
    enabled: bool = True  # Whether this source is enabled


def get_default_skill_sources() -> List[SkillSourceConfig]:
    """Get default skill source configurations.

    Priority levels:
    - Built-in (priority=0): skill_bundles/
    - Admin (priority=75): /app/storage/admin_skills/
    - User (priority=50): ~/.hugagent/skills/
    - Project (priority=100): .hugagent/skills/

    Environment variables:
    - HUGAGENT_ADMIN_SKILLS_DIR: Override admin skills directory
    - HUGAGENT_USER_SKILLS_DIR: Override user skills directory
    - HUGAGENT_PROJECT_SKILLS_DIR: Override project skills directory
    - HUGAGENT_DISABLE_ADMIN_SKILLS: Disable admin skills (set to "1" or "true")
    - HUGAGENT_DISABLE_USER_SKILLS: Disable user skills (set to "1" or "true")
    - HUGAGENT_DISABLE_PROJECT_SKILLS: Disable project skills (set to "1" or "true")

    Returns:
        List of SkillSourceConfig in priority order (lowest to highest).
    """
    sources: List[SkillSourceConfig] = []

    # 1. Built-in skills (always enabled)
    # The default built-in skill bundles live at ``src/backend/skill_bundles/default/`` (moved out of core/ after the repo refactor).
    # skill_bundles has two directories: ``default/`` = default always-on built-in skills;
    # ``marketplace/`` = install-based skill marketplace (scanned separately by marketplace_service, not within this load source).
    # This file is in ``core/agent_skills/``; go up three levels to ``src/backend`` then into ``skill_bundles/default``.
    builtin_dir = Path(__file__).parent.parent.parent / "skill_bundles" / "default"
    sources.append(
        SkillSourceConfig(
            name="built-in",
            root_dir=builtin_dir.resolve(),
            priority=0,
            enabled=True,
        )
    )

    # 2. Admin skills (managed via admin backend)
    admin_skills_dir = os.getenv(
        "HUGAGENT_ADMIN_SKILLS_DIR",
        "/app/storage/admin_skills/",
    )
    admin_disabled = os.getenv("HUGAGENT_DISABLE_ADMIN_SKILLS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    sources.append(
        SkillSourceConfig(
            name="admin",
            root_dir=Path(admin_skills_dir).expanduser().resolve(),
            priority=75,
            enabled=not admin_disabled,
        )
    )

    # 3. User skills
    user_skills_dir = os.getenv(
        "HUGAGENT_USER_SKILLS_DIR",
        "~/.hugagent/skills",
    )
    user_disabled = os.getenv("HUGAGENT_DISABLE_USER_SKILLS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    sources.append(
        SkillSourceConfig(
            name="user",
            root_dir=Path(user_skills_dir).expanduser().resolve(),
            priority=50,
            enabled=not user_disabled,
        )
    )

    # 4. Project skills
    project_skills_dir = os.getenv(
        "HUGAGENT_PROJECT_SKILLS_DIR",
        ".hugagent/skills",
    )
    project_disabled = os.getenv("HUGAGENT_DISABLE_PROJECT_SKILLS", "").lower() in (
        "1",
        "true",
        "yes",
    )
    sources.append(
        SkillSourceConfig(
            name="project",
            root_dir=Path(project_skills_dir).expanduser().resolve(),
            priority=100,
            enabled=not project_disabled,
        )
    )

    return sources


def get_enabled_skill_sources() -> List[SkillSourceConfig]:
    """Get only enabled skill sources.

    Returns:
        List of enabled SkillSourceConfig in priority order.
    """
    return [src for src in get_default_skill_sources() if src.enabled]


def get_sandbox_skills_dir() -> Path:
    """Unified host-backed directory that holds EVERY skill's files.

    Built-in skills are synced in at startup (see
    ``sync_builtin_skills_to_sandbox_dir``) and DB/admin skills are materialized
    here on demand (see ``loader._materialize_skill_files``). A single read-only
    bind mount exposes this whole directory inside the sandbox at
    ``/workspace/skills/<id>`` (see ``opensandbox_provider._make_skills_volume``),
    so built-in and imported skills share one in-sandbox path.

    Defaults under the storage volume (``$STORAGE_PATH/sandbox_skills``, which
    maps to ``$HOST_STORAGE_PATH/sandbox_skills`` on the host) so the OpenSandbox
    server can bind it — the same plumbing myspace uses. Override with
    ``SANDBOX_SKILLS_DIR``. Falls back to ``~/.cache`` only when the storage dir
    isn't writable (e.g. local unit tests outside Docker).

    The returned directory is always created — this is the single place that
    ensures it exists, so callers (loader materialize, the volume builder, the
    startup sync) don't each re-guard.
    """
    explicit = os.getenv("SANDBOX_SKILLS_DIR", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
    else:
        storage = os.getenv("STORAGE_PATH", "").strip() or "/app/storage"
        candidate = Path(storage) / "sandbox_skills"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate.resolve()
    except Exception:  # noqa: BLE001 — non-Docker/local fallback
        fallback = Path.home() / ".cache" / "hugagent" / "skills"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback.resolve()


def sync_builtin_skills_to_sandbox_dir() -> int:
    """Copy built-in skill folders into the unified sandbox skills dir.

    Built-in skills live in the read-only, git-tracked source tree, which we no
    longer bind-mount into the sandbox directly (that mount couldn't also hold
    DB skills). Instead we copy them once per startup into the unified dir so a
    single mount exposes built-in + DB skills at the same
    ``/workspace/skills/<id>`` path. Cheap (~3 MB); idempotent — overlays each
    skill dir so edits propagate on restart. Returns the number copied.
    """
    import shutil

    dest_root = get_sandbox_skills_dir()  # guaranteed to exist

    count = 0
    for src in get_default_skill_sources():
        if src.name != "built-in" or not src.root_dir.is_dir():
            continue
        for skill_dir in sorted(src.root_dir.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
                continue
            dest = dest_root / skill_dir.name
            try:
                shutil.copytree(
                    skill_dir,
                    dest,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
                )
                count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("[skills-sync] copy '%s' failed: %s", skill_dir.name, exc)
    logger.info("[skills-sync] synced %d built-in skills → %s", count, dest_root)
    return count
