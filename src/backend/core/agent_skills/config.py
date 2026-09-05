"""Configuration for multi-source skill loading."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

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

    # 5. Cloud skills synced into the desktop hybrid local runtime. The cloud
    #    is the source of truth there, so this source outranks every local one.
    from core.auth.desktop_bridge import bridge_enabled

    if bridge_enabled():
        sources.append(
            SkillSourceConfig(
                name="cloud",
                root_dir=get_cloud_skills_dir(),
                priority=200,
                enabled=True,
            )
        )

    return sources


def get_enabled_skill_sources() -> List[SkillSourceConfig]:
    """Get only enabled skill sources.

    Returns:
        List of enabled SkillSourceConfig in priority order.
    """
    return [src for src in get_default_skill_sources() if src.enabled]


def get_cloud_skills_dir() -> Path:
    """Where the desktop local runtime keeps the cloud skill snapshots (sibling of the shared dir)."""
    shared = get_sandbox_skills_dir()
    root = shared.parent / f"{shared.name}_cloud"
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_sandbox_skills_dir() -> Path:
    """Host-backed directory holding the files of every **shared** skill.

    Built-in skills are synced in at startup (see
    ``sync_builtin_skills_to_sandbox_dir``) and global DB/admin skills are
    materialized here on demand (see ``loader._materialize_skill_files``).
    Private skills go to their owner's dir instead (``get_user_skills_dir``) —
    see the layout note further down — and both surface inside the sandbox at
    the one path ``/workspace/skills/<id>``.

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


# ── 技能文件在沙箱里的两层布局 ────────────────────────────────────────────
# 公共技能（内置 + owner_user_id 为空的全局技能）放共享目录 get_sandbox_skills_dir()；
# 私有技能（用户自己创建/从市场安装的）放各自的用户目录 get_user_skills_dir(uid)。
# 沙箱里挂的是**用户目录**（/workspace/skills），公共技能以相对软链
# ``<id> -> ../skills_shared/<id>`` 出现在里面，共享目录另挂到 /workspace/skills_shared。
# 相对软链在三处都解析得到同一份文件：宿主机（skills_shared -> ../sandbox_skills）、
# opensandbox 沙箱、script-runner 会话工作区。因此一个用户的沙箱里只有公共技能加他
# 自己的私有技能，别人的私有技能（含市场技能安装时写入的 secrets.json）不可见，
# 而且不复制任何文件、不随用户数增长占盘。
SHARED_LINK_NAME = "skills_shared"


def get_user_skills_root() -> Path:
    """Root holding every user's private skill dir + skill view; sibling of the shared dir."""
    shared = get_sandbox_skills_dir()
    root = shared.parent / f"{shared.name}_u"
    root.mkdir(parents=True, exist_ok=True)
    link = root / SHARED_LINK_NAME
    # Relative so the same link works from the host, from inside a sandbox and
    # from the script-runner container — see the layout note above.
    if not link.is_symlink():
        try:
            link.symlink_to(Path("..") / shared.name, target_is_directory=True)
        except FileExistsError:
            pass
        except OSError as exc:  # noqa: BLE001
            logger.warning("[skills-view] shared link creation failed: %s", exc)
    return root


def _safe_dir_name(user_id: Optional[str]) -> str:
    """Return a user id usable as a directory name, or "" when it isn't one."""
    value = (user_id or "").strip()
    if not value or value in (".", "..", SHARED_LINK_NAME):
        return ""
    if "/" in value or "\\" in value or value.startswith("."):
        return ""
    return value


def get_user_skills_dir(user_id: Optional[str]) -> Optional[Path]:
    """The mounted-into-the-sandbox skill dir of one user, or None for an unusable id."""
    name = _safe_dir_name(user_id)
    if not name:
        return None
    return get_user_skills_root() / name


def skill_files_dir(skill_id: str, owner_user_id: Optional[str] = None) -> Path:
    """Where a skill's files are materialized: the owner's dir for private skills, the shared dir otherwise."""
    owner_dir = get_user_skills_dir(owner_user_id)
    return (owner_dir if owner_dir is not None else get_sandbox_skills_dir()) / skill_id


def sync_user_skill_view(user_id: Optional[str]) -> Optional[Path]:
    """Refresh one user's skill view and return the dir to mount at /workspace/skills.

    The view holds the user's own private skill dirs (real, written there by the
    loader) plus one relative symlink per shared skill. Only the symlinks are
    maintained here: added shared skills get a link, removed ones lose theirs.
    Costs one listdir plus a handful of symlink calls (no file is copied), so it
    is cheap enough to run on every sandbox creation.
    """
    view = get_user_skills_dir(user_id)
    if view is None:
        return None
    view.mkdir(parents=True, exist_ok=True)
    shared = get_sandbox_skills_dir()

    wanted = {d.name for d in shared.iterdir() if d.is_dir()}
    have = {e.name for e in view.iterdir() if e.is_symlink()}
    for name in wanted - have:
        try:
            (view / name).symlink_to(Path("..") / SHARED_LINK_NAME / name, target_is_directory=True)
        except FileExistsError:  # a real dir of the same name wins (private skill)
            pass
        except OSError as exc:  # noqa: BLE001
            logger.warning("[skills-view] link '%s' failed: %s", name, exc)
    for name in have - wanted:
        try:
            (view / name).unlink()
        except OSError as exc:  # noqa: BLE001
            logger.warning("[skills-view] unlink stale '%s' failed: %s", name, exc)
    return view


def purge_skill_sandbox_files(skill_id: str) -> bool:
    """Remove a deleted skill's materialized files, returning True when something was removed.

    Deleting a skill used to only delete its database row, leaving the files it
    was materialized into on disk — and those are mounted into sandboxes, so the
    scripts (and, for a market skill installed with credentials, secrets.json)
    of a deleted skill stayed readable. Every skill deletion path must call this.

    Built-in skills are skipped: their directory is a startup copy of the
    git-tracked bundle shared by all users, not owned by the deleted row. The
    owner is not needed — a skill id is unique, so we clear it from the shared
    dir and from every user dir.
    """
    import shutil

    skill_id = (skill_id or "").strip()
    if not skill_id or "/" in skill_id or "\\" in skill_id or skill_id.startswith("."):
        return False
    if (_builtin_skills_dir() / skill_id).is_dir():
        return False

    removed = False
    roots = [get_sandbox_skills_dir()] + [
        d for d in get_user_skills_root().iterdir() if d.is_dir() and not d.is_symlink()
    ]
    for root in roots:
        target = root / skill_id
        try:
            if target.is_symlink():  # a view link to a shared skill, not this skill's files
                continue
            if not target.is_dir() or target.resolve().parent != root.resolve():
                continue
            shutil.rmtree(target)
            removed = True
        except OSError as exc:  # noqa: BLE001 — cleanup must never fail a deletion
            logger.warning("[skills-purge] remove '%s' from %s failed: %s", skill_id, root, exc)
    if removed:
        logger.info("[skills-purge] removed sandbox files for skill '%s'", skill_id)
    return removed


def _builtin_skills_dir() -> Path:
    return Path(__file__).parent.parent.parent / "skill_bundles" / "default"


def prune_orphan_sandbox_skill_dirs(live_skill_owners: dict) -> int:
    """Drop materialized dirs that no longer match a live skill, returning the number removed.

    Sweeps three kinds of leftovers: skills deleted before the purge-on-delete
    path existed, and — after the introduction of per-user dirs — private skills
    still sitting in the shared dir plus dirs under the wrong owner. A skill
    whose files are dropped while still live re-materializes on demand, so the
    sweep is safe; built-in bundles are re-synced at every startup.
    """
    import shutil

    builtin = {d.name for d in _builtin_skills_dir().iterdir() if d.is_dir()}
    removed = 0

    shared = get_sandbox_skills_dir()
    for entry in shared.iterdir():
        if not entry.is_dir() or entry.is_symlink() or entry.name in builtin:
            continue
        # Only skills with no owner belong in the shared dir.
        if entry.name in live_skill_owners and not live_skill_owners[entry.name]:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1

    for user_dir in get_user_skills_root().iterdir():
        if user_dir.name == SHARED_LINK_NAME or not user_dir.is_dir() or user_dir.is_symlink():
            continue
        for entry in user_dir.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if live_skill_owners.get(entry.name) == user_dir.name:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1

    if removed:
        logger.info("[skills-purge] pruned %d stale skill dirs", removed)
    return removed
