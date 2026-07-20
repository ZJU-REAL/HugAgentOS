"""Skill Marketplace service.

The skill marketplace is a set of **preloaded, curated** installable skill
packages, shipped with the repo/image and stored under
``skill_bundles/marketplace/<slug>/`` (each directory holds the original
SKILL.md + referenced files + a ``marketplace.json`` manifest). The built-in
loader scans ``skill_bundles/*/SKILL.md`` with single-level matching; market
skills live two levels deep at ``marketplace/<slug>/SKILL.md`` and are NOT
auto-loaded as built-ins — so before "installation" a market skill never
appears in the catalog nor gets registered with the agent. Only after a
user/admin explicitly installs it does it take effect in the DB.

Installation reuses the existing ``AdminSkill`` mechanism:
- Admin install → global skill (``owner_user_id`` empty), skill id = manifest entry_name;
- User install  → private skill (``owner_user_id`` = current user); the skill id
  gets a user-fingerprint suffix appended to guarantee global uniqueness, so
  multiple users can each install the same market skill, each with their own
  credentials.

Credentials (API keys etc.): the manifest may declare ``required_secrets``;
they are collected by the caller at install time, written to ``secrets.json``
in that installed skill's directory, and a "credentials configuration" section
is appended at the end of SKILL.md for the agent to read when running scripts.
The marketplace directory itself stores NO secrets.

Community publishing (marketplace_submissions): users can **submit** their
private skills for listing in the marketplace; once an admin approves them at
/admin, the skill appears in the market list with ``source=community`` and
becomes installable by everyone. Submission snapshots the source skill's
content, decoupling the listed content from the user's later edits; an admin
rejecting an already-approved submission delists it.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.agent_skills.binary_files import (
    BINARY_EXTENSIONS,
    JUNK_BASENAMES,
    MAX_SINGLE_FILE,
    MAX_TOTAL,
    decode_binary,
    encode_binary,
    is_binary_value,
    pack_directory,
)
from core.agent_skills.cache_refresh import refresh_skill_caches
from core.agent_skills.deps_detector import detect_dependencies
from core.agent_skills.registry import (
    SkillSpecError,
    _load_skill_metadata_from_str,
    _split_frontmatter,
)
from core.db.models import AdminSkill, MarketplaceSubmission
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.ontology.build_validator import ensure_ontology_build_valid

logger = logging.getLogger(__name__)

# Root directory of preloaded market skill packages: ``src/backend/skill_bundles/marketplace/``.
# Lives in the same tree as built-in skills (``skill_bundles/<slug>/``), making
# additions/removals uniform under skill_bundles; but the built-in loader's scan
# is single-level ``glob("*/SKILL.md")`` — market skills at
# ``marketplace/<slug>/SKILL.md`` (two levels deep) are not auto-loaded as
# built-ins, preserving the install-based model.
MARKETPLACE_DIR = Path(__file__).resolve().parents[2] / "skill_bundles" / "marketplace"
# Built-in default skills directory: ``skill_bundles/default/<slug>/``. These
# skills are globally resident and always available to everyone; they are also
# shown in the skill marketplace as "built-in" entries (read from their own
# folders, not copied into marketplace/ to avoid 4MB+ duplication).
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parents[2] / "skill_bundles" / "default"

MANIFEST_NAME = "marketplace.json"
SKILL_MD_NAME = "SKILL.md"
SECRETS_FILENAME = "secrets.json"

# The built-in default skills' "category + summary" in the marketplace (default
# skills have no category in their frontmatter; the Chinese name comes from
# SKILL.md's display_name). The key set defines "which default skills are listed
# in the marketplace" — visible to everyone, marked as built-in resident.
DEFAULT_SKILL_MARKET = {
    "excel-editing":  {"category": "办公效率", "summary": "生成 / 编辑 Excel 工作簿：建表、套公式、加图表、导出 .xlsx"},
    "word-editing":   {"category": "文档处理", "summary": "生成 / 编辑 Word 文档：起草、排版、套模板、替换占位符、导出 .docx"},
    "pdf-editing":    {"category": "文档处理", "summary": "生成 / 合并 / 拆分 / 抽页 / 填表单 / 读取 PDF 文档"},
    "ppt-design":     {"category": "办公效率", "summary": "设计 / 生成 / 编辑 PPT 演示文稿，提纲扩写成整套 .pptx"},
    "capability-guide-brief": {"category": "办公效率", "summary": "速答系统能力清单与使用指引"},
}

# Fixed skill-marketplace categories (the only allowed set): preloaded
# manifests, community submissions, and admin-review category changes may only
# take these 8 values; mirrored on the frontend in utils/constants.ts as
# MARKETPLACE_CATEGORIES.
MARKETPLACE_CATEGORIES = [
    "写作助手", "文档处理", "数据分析", "政策产业",
    "营销创意", "法务合规", "办公效率", "研发效率",
]
# Storage constraints / binary extension set + packing logic are unified in
# core.agent_skills.binary_files (pack_directory / BINARY_EXTENSIONS / MAX_*);
# admin upload and plugin import share the same single source of truth.


# ── Manifest / listing ───────────────────────────────────────────────────────

def _read_manifest(slug: str) -> Optional[Dict[str, Any]]:
    """Read and validate a single market skill's ``marketplace.json`` (tolerant; returns None if corrupted)."""
    if not slug or "/" in slug or ".." in slug:
        return None
    path = MARKETPLACE_DIR / slug / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("marketplace manifest broken: %s (%s)", slug, exc)
        return None
    if not isinstance(m, dict) or not m.get("entry_name"):
        return None
    m.setdefault("slug", slug)
    return m


def market_skill_requires_secrets(slug: str) -> bool:
    """Whether a preloaded market skill declares required_secrets (public contract, reusable by other markets/callers)."""
    m = _read_manifest(slug)
    if not m:
        return False
    return bool(m.get("requires_api_key") or m.get("required_secrets"))


def _public_meta(m: Dict[str, Any]) -> Dict[str, Any]:
    """Manifest → externally visible metadata (no skill body/file contents)."""
    return {
        "slug": m.get("slug"),
        "entry_name": m.get("entry_name"),
        "display_name": m.get("display_name") or m.get("slug"),
        "summary": m.get("summary") or "",
        "category": m.get("category") or "其他",
        "tags": list(m.get("tags") or []),
        "version": m.get("version") or "1.0.0",
        "author": m.get("author") or "skillhub",
        "icon_url": m.get("icon_url") or "",
        "source": m.get("source") or "skillhub",
        "source_url": m.get("source_url") or "",
        "downloads": int(m.get("downloads") or 0),
        "stars": int(m.get("stars") or 0),
        "featured": bool(m.get("featured")),
        "requires_api_key": bool(m.get("requires_api_key")),
        "required_secrets": list(m.get("required_secrets") or []),
        # Preloaded (filesystem) skills ship with the repo; admins cannot delete them online.
        "deletable": False,
    }


def _submission_public_meta(sub: MarketplaceSubmission) -> Dict[str, Any]:
    """Approved community submission → external metadata isomorphic to the preloaded manifest form."""
    return {
        "slug": sub.slug,
        "entry_name": sub.slug,
        "display_name": sub.display_name or sub.slug,
        "summary": sub.summary or "",
        "category": sub.category or "社区共享",
        "tags": list(sub.tags or []),
        "version": sub.version or "1.0.0",
        "author": sub.submitter_name or sub.owner_user_id,
        "icon_url": "",
        "source": "community",
        "source_url": "",
        "downloads": 0,
        "stars": 0,
        "featured": False,
        "requires_api_key": False,
        "required_secrets": _submission_required_secrets(sub),
        # DB listing records (admin uploads / user community submissions) → admins can delete online (remove from the market).
        "deletable": True,
    }


def _approved_submissions(db: Session) -> List[MarketplaceSubmission]:
    return (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.status == "approved")
        .order_by(MarketplaceSubmission.created_at.desc())
        .all()
    )


def _default_skill_public_meta(slug: str) -> Optional[Dict[str, Any]]:
    """Built-in default skill (skill_bundles/default/<slug>) → market-list metadata isomorphic to the preloaded form.

    The Chinese name comes from SKILL.md's ``display_name``; category/summary
    come from ``DEFAULT_SKILL_MARKET``. Marked ``source='builtin'`` +
    ``builtin=True`` — these skills are globally resident and always available
    to everyone; the market shows them as "built-in" (already available) and
    they do not go through "install as a private copy" (avoiding duplicating
    1MB+ office skills into the DB).
    """
    cfg = DEFAULT_SKILL_MARKET.get(slug)
    if cfg is None:
        return None
    md = DEFAULT_SKILLS_DIR / slug / SKILL_MD_NAME
    if not md.is_file():
        return None
    try:
        fm, _ = _split_frontmatter(md.read_text(encoding="utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        fm = {}
    name = str(fm.get("name") or slug).strip()
    tags = fm.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return {
        "slug": slug,
        "entry_name": name,
        "display_name": str(fm.get("display_name") or name).strip(),
        "summary": cfg["summary"],
        "category": cfg["category"],
        "tags": list(tags),
        "version": str(fm.get("version") or "1.0.0"),
        "author": "内置",
        "icon_url": "",
        "source": "builtin",
        "source_url": "",
        "downloads": 0,
        "stars": 0,
        "featured": False,
        "requires_api_key": False,
        "required_secrets": [],
        "deletable": False,  # built-ins ship with the repo; not deletable online
        "builtin": True,     # the frontend uses this to show "built-in" and set the button to "already built-in"
    }


def _default_skill_metas() -> List[Dict[str, Any]]:
    metas = [_default_skill_public_meta(s) for s in DEFAULT_SKILL_MARKET]
    return [m for m in metas if m]


def list_marketplace_skills(
    db: Optional[Session] = None,
    *,
    include_disabled: bool = False,
    viewer_user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all market skill metadata: built-in default skills + preloaded (featured first, downloads descending) + community submissions.

    ``include_disabled``: the admin console passes True (sees everything +
    ``market_enabled`` annotation); the user side defaults to False (only sees
    listed items, and scoped items invisible to ``viewer_user_id`` are filtered
    by visibility). See ``marketplace_listing`` for the delist switch and
    visibility scopes.
    """
    items: List[Dict[str, Any]] = []
    if MARKETPLACE_DIR.is_dir():
        for child in sorted(MARKETPLACE_DIR.iterdir()):
            if not child.is_dir():
                continue
            m = _read_manifest(child.name)
            if m:
                items.append(_public_meta(m))
    items.sort(key=lambda x: (0 if x["featured"] else 1, -x["downloads"], x["slug"]))
    # Built-in default skills come first (built-in resident, shown with priority).
    items = _default_skill_metas() + items
    if db is not None:
        items.extend(_submission_public_meta(s) for s in _approved_submissions(db))
        from core.services import marketplace_listing as ml
        items = ml.annotate_and_filter(
            db, ml.KIND_SKILL, items, id_key="slug",
            include_disabled=include_disabled, viewer_user_id=viewer_user_id,
        )
    return items


def set_marketplace_skill_enabled(
    db: Session, slug: str, enabled: bool, *, updated_by: Optional[str] = None
) -> Dict[str, Any]:
    """List/delist a market skill (controls whether it shows in the skill marketplace; installed instances are unaffected)."""
    from core.services import marketplace_listing as ml
    return ml.set_listing_enabled(db, ml.KIND_SKILL, slug, enabled, updated_by=updated_by)


def list_categories(
    db: Optional[Session] = None,
    *,
    include_disabled: bool = False,
    viewer_user_id: Optional[str] = None,
) -> List[str]:
    """Marketplace category list: the 8 fixed categories first (stable order), legacy leftover categories appended after."""
    seen: List[str] = list(MARKETPLACE_CATEGORIES)
    for it in list_marketplace_skills(
        db, include_disabled=include_disabled, viewer_user_id=viewer_user_id
    ):
        if it["category"] not in seen:
            seen.append(it["category"])
    return seen


def _validate_category(category: str) -> str:
    """Validate and return a legal category; anything outside the fixed set is a straight 400."""
    category = (category or "").strip()
    if category not in MARKETPLACE_CATEGORIES:
        raise BadRequestError(
            message=f"请从固定分类中选择：{'、'.join(MARKETPLACE_CATEGORIES)}"
        )
    return category


def _get_approved_submission(db: Session, slug: str) -> Optional[MarketplaceSubmission]:
    return (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.slug == slug, MarketplaceSubmission.status == "approved")
        .first()
    )


def _strip_frontmatter(raw: str) -> str:
    try:
        _, body = _split_frontmatter(raw)
    except Exception:  # noqa: BLE001
        body = raw
    return (body or "").strip()


def get_marketplace_skill(slug: str, db: Optional[Session] = None) -> Dict[str, Any]:
    """Single market skill detail: metadata + file list + SKILL.md body (for the detail preview).

    The preloaded directory takes priority; when not found, fall back to the
    community listing (approved submission).
    """
    # Built-in default skills: read skill_bundles/default/<slug>; the body comes from SKILL.md, the file list from the directory.
    if slug in DEFAULT_SKILL_MARKET:
        meta = _default_skill_public_meta(slug)
        if meta is None:
            raise ResourceNotFoundError("marketplace_skill", slug)
        pkg_dir = DEFAULT_SKILLS_DIR / slug
        files: List[Dict[str, Any]] = []
        for p in sorted(pkg_dir.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(pkg_dir).as_posix()
            if rel == SKILL_MD_NAME:
                continue
            files.append({"path": rel, "size": p.stat().st_size})
        skill_md = pkg_dir / SKILL_MD_NAME
        meta["files"] = files
        meta["instructions"] = _strip_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore")) if skill_md.is_file() else ""
        return meta
    m = _read_manifest(slug)
    if not m:
        sub = _get_approved_submission(db, slug) if db is not None else None
        if sub is None:
            raise ResourceNotFoundError("marketplace_skill", slug)
        data = _submission_public_meta(sub)
        data["files"] = [
            {"path": rel, "size": len(str(content).encode("utf-8"))}
            for rel, content in sorted((sub.extra_files or {}).items())
            if rel != REQUIRED_SECRETS_SNAPSHOT  # internal sentinel, not part of the installed content
        ]
        data["instructions"] = _strip_frontmatter(sub.skill_content or "")
        return data
    pkg_dir = MARKETPLACE_DIR / slug
    data = _public_meta(m)

    files: List[Dict[str, Any]] = []
    for p in sorted(pkg_dir.rglob("*")):
        if not p.is_file() or p.name == MANIFEST_NAME:
            continue
        rel = p.relative_to(pkg_dir).as_posix()
        if rel == SKILL_MD_NAME:  # SKILL.md is returned separately as instructions; don't duplicate it in the file list
            continue
        files.append({"path": rel, "size": p.stat().st_size})
    body = ""
    skill_md = pkg_dir / SKILL_MD_NAME
    if skill_md.is_file():
        body = _strip_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
    data["files"] = files
    data["instructions"] = body
    return data


# ── Installation ─────────────────────────────────────────────────────────────

def _user_suffix(owner_user_id: str) -> str:
    """Stable 6-char fingerprint derived from user_id, used to deduplicate private-install skill ids."""
    return hashlib.sha1(owner_user_id.encode("utf-8")).hexdigest()[:6]


def compute_install_id(entry_name: str, owner_user_id: Optional[str]) -> str:
    """The installed AdminSkill.skill_id: admin = original name (global); user = original name + fingerprint (private)."""
    if owner_user_id is None:
        return entry_name
    return f"{entry_name}-{_user_suffix(owner_user_id)}"


def base_entry_name(skill_id: str, owner_user_id: Optional[str]) -> str:
    """Inverse of ``compute_install_id``: recover the underlying market entry_name from an install id.

    Private install ids look like ``{entry_name}-{fingerprint}``; stripping that
    user's 6-char fingerprint suffix yields the entry_name. Global ids (owner is
    None) already are the entry_name and are returned as-is. Used to merge the
    "privately installed version" and the "admin global version" onto the same
    underlying skill for deduplication — preventing the same skill from
    appearing twice in the skill library / at runtime.
    """
    if owner_user_id is None:
        return skill_id
    suffix = f"-{_user_suffix(owner_user_id)}"
    if skill_id.endswith(suffix):
        return skill_id[: -len(suffix)]
    return skill_id


def is_installed(db: Session, slug: str, owner_user_id: Optional[str]) -> bool:
    """Whether this market skill has been installed by the given scope (global / current user)."""
    if slug in DEFAULT_SKILL_MARKET:
        return True  # built-in default skills are globally resident; considered always "installed"
    m = _read_manifest(slug)
    if not m:
        if _get_approved_submission(db, slug) is None:
            return False
        entry_name = slug
    else:
        entry_name = m["entry_name"]
    install_id = compute_install_id(entry_name, owner_user_id)
    q = db.query(AdminSkill).filter(AdminSkill.skill_id == install_id)
    if owner_user_id is None:
        q = q.filter(AdminSkill.owner_user_id.is_(None))
    else:
        q = q.filter(AdminSkill.owner_user_id == owner_user_id)
    return q.first() is not None


def annotate_installed(
    items: List[Dict[str, Any]], db: Session, owner_user_id: Optional[str]
) -> List[Dict[str, Any]]:
    """Annotate each market list item with an ``installed`` flag (for the given scope). One batched query decides all."""
    if not items:
        return items
    install_ids = {
        it["slug"]: compute_install_id(it["entry_name"], owner_user_id) for it in items
    }
    q = db.query(AdminSkill.skill_id, AdminSkill.dep_status)
    if owner_user_id is None:
        q = q.filter(AdminSkill.owner_user_id.is_(None))
    else:
        q = q.filter(AdminSkill.owner_user_id == owner_user_id)
    q = q.filter(AdminSkill.skill_id.in_(list(install_ids.values())))
    status_map = {sid: dep for sid, dep in q.all()}
    # Rejected skills carry the admin's reason, surfaced to the user ("Rejected by admin: ...").
    rejected_ids = [sid for sid, dep in status_map.items() if dep == "rejected"]
    reason_map: Dict[str, Optional[str]] = {}
    if rejected_ids:
        from core.services.skill_deps_request_service import get_reject_reason
        for sid in rejected_ids:
            reason_map[sid] = get_reject_reason(db, sid)
    for it in items:
        # Built-in default skills are globally resident and always available to
        # everyone — mark them "installed/ready" directly (the frontend button
        # shows "already built-in"); no AdminSkill lookup, no install flow.
        if it.get("builtin"):
            it["installed"] = True
            it["dep_status"] = "ready"
            it["dep_reason"] = None
            continue
        iid = install_ids[it["slug"]]
        it["installed"] = iid in status_map
        # dep_status: 'installing' = dependencies installing / 'rejected' = admin declined / 'ready' = usable. None when not installed.
        it["dep_status"] = status_map.get(iid) if iid in status_map else None
        it["dep_reason"] = reason_map.get(iid)
    return items


def _rewrite_frontmatter_name(content: str, new_name: str) -> str:
    """Rewrite the ``name:`` in the SKILL.md frontmatter to ``new_name`` (touching only that line).

    Replaces only the first ``name:`` line within the frontmatter block; all
    other content (including multi-line descriptions) is preserved verbatim.
    Returned unchanged when there is no frontmatter.
    """
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    head = content[: end + 1]
    tail = content[end + 1 :]
    head = re.sub(r"(?m)^name:[ \t]*.*$", f"name: {new_name}", head, count=1)
    return head + tail


def _load_package_files(pkg_dir: Path) -> Tuple[str, Dict[str, str]]:
    """Read a market skill package: returns (SKILL.md original text, {relative path: stored content}).

    Binary files are stored as base64 (consistent with admin upload), text as
    original text; the manifest and junk files are skipped, and per-file/total
    size caps are enforced.
    """
    skill_md = pkg_dir / SKILL_MD_NAME
    if not skill_md.is_file():
        raise BadRequestError(message="市场技能包缺少 SKILL.md")
    skill_content = skill_md.read_text(encoding="utf-8")
    extra_files = pack_directory(pkg_dir, skip_names={MANIFEST_NAME, SKILL_MD_NAME})
    return skill_content, extra_files


def _inject_secrets(
    skill_content: str,
    extra_files: Dict[str, str],
    required_secrets: List[Dict[str, Any]],
    secrets: Dict[str, str],
) -> str:
    """Write the user-provided credentials into secrets.json and append a "credentials configuration" section at the end of SKILL.md.

    Returns the SKILL.md content with the section appended. Missing required
    fields raise a 400.
    """
    cleaned: Dict[str, str] = {}
    for field in required_secrets:
        key = str(field.get("key") or "").strip()
        if not key:
            continue
        val = (secrets.get(key) or "").strip()
        if not val:
            if field.get("required"):
                label = field.get("label") or key
                raise BadRequestError(message=f"请填写必填凭据：{label}")
            continue
        cleaned[key] = val
    if not cleaned:
        return skill_content

    extra_files[SECRETS_FILENAME] = json.dumps(cleaned, ensure_ascii=False, indent=2)
    lines = [
        "",
        "---",
        "",
        "## 🔑 凭据配置（由安装者提供）",
        "",
        f"运行本技能所需的凭据已写入技能目录下的 `{SECRETS_FILENAME}`，包含以下字段：",
        "",
    ]
    for key in cleaned:
        lines.append(f"- `{key}`")
    lines += [
        "",
        f"执行技能脚本前，请先从 `{SECRETS_FILENAME}` 读取这些值，并以**同名环境变量**导出后再运行，例如：",
        "",
        "```bash",
        f'export {next(iter(cleaned))}="$(python3 -c "import json;print(json.load(open(\'{SECRETS_FILENAME}\'))[\'{next(iter(cleaned))}\'])")"',
        "```",
        "",
        "> 凭据仅存于本技能目录，请勿在对话中明文回显。",
        "",
    ]
    return skill_content.rstrip() + "\n" + "\n".join(lines)


_SECRETS_SECTION_HEADER = "## 🔑 凭据配置（由安装者提供）"
# Internal sentinel filename carrying required_secrets inside listing snapshots:
# stored with the snapshot, popped before install/detail display, never lands in
# the installed skill directory.
REQUIRED_SECRETS_SNAPSHOT = "_required_secrets.json"


def _strip_injected_secrets(skill_content: str) -> str:
    """Strip the "credentials configuration" section appended by _inject_secrets (including its preceding --- separator).

    Listing snapshots must be in the pre-injection form — the section instructs
    reading secrets.json, which the snapshot has already excluded; the
    installer's own credentials get a fresh section re-injected at install time.
    """
    idx = skill_content.find(_SECRETS_SECTION_HEADER)
    if idx < 0:
        return skill_content
    sep = skill_content.rfind("\n---", 0, idx)
    cut = sep if sep >= 0 else idx
    return skill_content[:cut].rstrip() + "\n"


def _submission_required_secrets(sub: MarketplaceSubmission) -> List[Dict[str, Any]]:
    """Recover a community skill's required_secrets from the snapshot sentinel entry (empty list if absent)."""
    raw = (sub.extra_files or {}).get(REQUIRED_SECRETS_SNAPSHOT)
    if not raw:
        return []
    try:
        fields = json.loads(raw)
        return fields if isinstance(fields, list) else []
    except (TypeError, ValueError):
        return []


def install_marketplace_skill(
    db: Session,
    slug: str,
    *,
    owner_user_id: Optional[str],
    secrets: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Install a market skill as an ``AdminSkill`` (admin = global / user = private), returning a result dict.

    The preloaded directory takes priority; when the slug isn't in the preloaded
    directory, fall back to the community listing (approved submission), with
    install content taken from the submission-time snapshot.
    """
    # Built-in default skills are globally resident and always available to
    # everyone — no install needed (the frontend button is already disabled as
    # "already built-in"); this is a defensive block to avoid duplicating 1MB+
    # office skill content into the DB as a private copy.
    if slug in DEFAULT_SKILL_MARKET:
        raise BadRequestError(message="内置技能已全局可用，无需安装")
    secrets = secrets or {}
    m = _read_manifest(slug)
    if m:
        pkg_dir = MARKETPLACE_DIR / slug
        skill_content, extra_files = _load_package_files(pkg_dir)
    else:
        sub = _get_approved_submission(db, slug)
        if sub is None:
            raise ResourceNotFoundError("marketplace_skill", slug)
        m = {
            "slug": sub.slug,
            "entry_name": sub.slug,
            "display_name": sub.display_name,
            "summary": sub.summary or "",
            "category": sub.category or "社区共享",
            "tags": list(sub.tags or []),
            "version": sub.version or "1.0.0",
            "required_secrets": _submission_required_secrets(sub),
        }
        skill_content = sub.skill_content or ""
        extra_files = dict(sub.extra_files or {})
        # The sentinel entry only carries the credential field names; it never lands in the install directory
        extra_files.pop(REQUIRED_SECRETS_SNAPSHOT, None)

    entry_name = str(m["entry_name"]).strip()
    install_id = compute_install_id(entry_name, owner_user_id)
    # Always rewrite the frontmatter ``name`` to the install id: this both
    # namespaces private user installs and, along the way, normalizes illegal
    # names in market skills (containing spaces/Chinese etc.) — otherwise
    # _load_skill_metadata_from_str fails validation with "invalid skill id".
    # install_id comes from slug/entry_name and is always a legal slug.
    skill_content = _rewrite_frontmatter_name(skill_content, install_id)

    required_secrets = list(m.get("required_secrets") or [])
    if required_secrets:
        skill_content = _inject_secrets(
            skill_content, extra_files, required_secrets, secrets
        )

    # Validate SKILL.md (description required, etc.); an invalid one is a straight 400 rather than being persisted.
    try:
        meta = _load_skill_metadata_from_str(skill_content, install_id)
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(message=f"市场技能 SKILL.md 不合法：{exc}")

    dependencies = detect_dependencies(
        {fn: c for fn, c in extra_files.items() if not is_binary_value(c)}
    )

    display_name = m.get("display_name") or meta.name or install_id
    description = meta.description or m.get("summary") or ""
    tags = list(m.get("tags") or meta.tags or [])
    version = m.get("version") or meta.version or "1.0.0"
    user_intro = _build_user_intro(m)

    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=display_name or install_id,
        description=description,
        instructions=skill_content,
        tool_names=list(meta.allowed_tools or []),
        ontology_tags=list(tags),
    )

    now = datetime.utcnow()
    existing = db.query(AdminSkill).filter(AdminSkill.skill_id == install_id).first()
    if existing is not None:
        # Owner conflict: cannot overwrite a public skill or another user's private skill (admin reinstalling a global one is allowed)
        if existing.owner_user_id != owner_user_id:
            if existing.owner_user_id is None:
                raise HTTPException(status_code=409, detail=f"技能 id 「{install_id}」与公共技能冲突")
            raise HTTPException(status_code=409, detail=f"技能 id 「{install_id}」已被占用")
        existing.skill_content = skill_content
        existing.display_name = display_name
        existing.description = description
        existing.user_intro = user_intro
        existing.version = version
        existing.tags = tags
        existing.allowed_tools = list(meta.allowed_tools or [])
        existing.extra_files = extra_files
        existing.dependencies = dependencies
        existing.is_enabled = True
        existing.updated_at = now
        flag_modified(existing, "tags")
        flag_modified(existing, "extra_files")
        flag_modified(existing, "dependencies")
        action = "updated"
    else:
        db.add(AdminSkill(
            skill_id=install_id,
            skill_content=skill_content,
            display_name=display_name,
            description=description,
            user_intro=user_intro,
            version=version,
            tags=tags,
            allowed_tools=list(meta.allowed_tools or []),
            extra_files=extra_files,
            dependencies=dependencies,
            is_enabled=True,
            owner_user_id=owner_user_id,
            created_at=now,
            updated_at=now,
        ))
        action = "installed"
    db.commit()
    # Strict dependency verdicts happen at the async route layer (gate_skill_deps
    # needs to actually run probes in the sandbox; this function is synchronous).
    # Here we only carry the declared dependencies out with the result; the route
    # awaits the probe → decides installing/ready.
    # Assign the installed skill a built-in icon by category (users/admins can change it later in skill editing).
    try:
        from core.services.skill_icon_service import (
            get_skill_icon,
            preset_for_category,
            set_skill_icon,
        )
        if not get_skill_icon(db, install_id):
            set_skill_icon(db, install_id, preset_for_category(m.get("category")))
    except Exception as exc:  # noqa: BLE001
        logger.debug("marketplace set default icon failed: %s", exc)
    refresh_skill_caches()
    logger.info(
        "marketplace_skill_%s: slug=%s id=%s owner=%s files=%d",
        action, slug, install_id, owner_user_id or "global", len(extra_files),
    )
    return {
        "id": install_id,
        "slug": slug,
        "owner": "self" if owner_user_id else "global",
        "action": action,
        "dependencies": dependencies,
        # dep_pending / message are filled in by the route layer after awaiting gate_skill_deps (strict probe result).
        "dep_pending": False,
        "message": "技能已安装" if action == "installed" else "技能已更新",
    }


def _build_user_intro(m: Dict[str, Any]) -> str:
    """Capability-center detail page intro: summary only (no source attribution appended)."""
    return (m.get("summary") or "").strip()


# ── Admin uploads a zip → listed directly in the skill marketplace ───────────
#
# Skills uploaded by admins from the /admin console no longer land directly as
# "globally enabled" AdminSkill rows; instead they are listed as a
# status='approved' MarketplaceSubmission — appearing in the skill marketplace
# and explicitly installable, but they do NOT enter the catalog and are not
# auto-loaded by the agent. Shares the same table as users' "submit for
# listing", just review-exempt (the admin IS the review authority).
# owner_user_id uses a fixed sentinel to distinguish "admin uploads" from real
# users' private listing submissions.
ADMIN_UPLOAD_OWNER = "__admin_upload__"


def parse_skill_zip(data: bytes) -> Dict[str, Any]:
    """Parse a skill zip → returns {skill_id, skill_content (original text), meta, extra_files, dependencies, skipped}.

    Parsing/validation only; nothing is persisted. The same unpacking logic is
    shared by admin market-listing uploads and users' private uploads.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid zip file")

    # Security: zip-slip
    for name in zf.namelist():
        if name.startswith("/") or ".." in name:
            raise HTTPException(status_code=400, detail=f"Unsafe path in zip: {name}")

    skill_md_paths = [n for n in zf.namelist() if n.endswith("SKILL.md")]
    if not skill_md_paths:
        raise HTTPException(status_code=400, detail="No SKILL.md found in zip")

    skill_md_path = skill_md_paths[0]
    parts = skill_md_path.split("/")
    if len(parts) == 1:
        prefix = ""
    elif len(parts) == 2:
        prefix = parts[0] + "/"
    else:
        prefix = "/".join(parts[:-1]) + "/"

    try:
        raw = zf.read(skill_md_path).decode("utf-8")
        fm, _ = _split_frontmatter(raw)
        skill_id = fm.get("name", "").strip()
        if not skill_id:
            if prefix:
                skill_id = prefix.rstrip("/").split("/")[-1]
            else:
                raise HTTPException(status_code=400, detail="SKILL.md missing 'name' in frontmatter")
    except SkillSpecError as e:
        raise HTTPException(status_code=400, detail=f"Invalid SKILL.md: {e}")

    try:
        meta = _load_skill_metadata_from_str(raw, skill_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid skill: {e}")

    JUNK_PATH_MARKERS = ("__MACOSX/", "/.git/", "/__pycache__/", "/.svn/", "/.hg/")
    extra_files: Dict[str, str] = {}
    skipped: List[Dict[str, str]] = []
    total_size = 0
    for entry in zf.namelist():
        if entry == skill_md_path or entry.endswith("/"):
            continue
        if prefix and not entry.startswith(prefix):
            continue
        rel_name = entry[len(prefix):] if prefix else entry
        if not rel_name:
            continue
        base = rel_name.rsplit("/", 1)[-1]
        if base in JUNK_BASENAMES or any(m in f"/{entry}" for m in JUNK_PATH_MARKERS):
            continue
        info = zf.getinfo(entry)
        if info.file_size > MAX_SINGLE_FILE:
            skipped.append({"file": rel_name, "reason": f"exceeds {MAX_SINGLE_FILE // (1024 * 1024)}MB single-file ceiling"})
            continue
        try:
            file_bytes = zf.read(entry)
        except (KeyError, zipfile.BadZipFile):
            skipped.append({"file": rel_name, "reason": "unreadable zip entry"})
            continue
        _, ext = os.path.splitext(rel_name)
        if ext.lower() in BINARY_EXTENSIONS:
            stored = encode_binary(file_bytes)
        else:
            try:
                stored = file_bytes.decode("utf-8")
            except UnicodeDecodeError:
                stored = encode_binary(file_bytes)
        stored_size = len(stored.encode("utf-8"))
        if total_size + stored_size > MAX_TOTAL:
            skipped.append({"file": rel_name, "reason": f"would exceed {MAX_TOTAL // (1024 * 1024)}MB total ceiling"})
            continue
        total_size += stored_size
        extra_files[rel_name] = stored

    dependencies = detect_dependencies(
        {fn: c for fn, c in extra_files.items() if not is_binary_value(c)}
    )
    return {
        "skill_id": skill_id,
        "skill_content": raw,
        "meta": meta,
        "extra_files": extra_files,
        "dependencies": dependencies,
        "skipped": skipped,
    }


def build_skill_zip(skill_id: str, skill_content: str, extra_files: Dict[str, str]) -> bytes:
    """Pack a DB skill into a zip byte stream (inverse of ``parse_skill_zip``).

    Layout is ``<skill_id>/SKILL.md`` + ``<skill_id>/<relative path>``,
    consistent with the upload/import convention, so an exported package can be
    re-imported directly. base64 values in extra_files carrying the binary
    marker are restored to their original bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_id}/SKILL.md", skill_content or "")
        for rel, stored in (extra_files or {}).items():
            data = decode_binary(stored) if is_binary_value(stored) else str(stored).encode("utf-8")
            zf.writestr(f"{skill_id}/{rel}", data)
    return buf.getvalue()


_ZIP_SKIP_DIR_PARTS = {"__pycache__", ".git", ".svn", ".hg", "__MACOSX"}


def build_skill_zip_from_dir(skill_id: str, skill_dir: Path) -> bytes:
    """Pack an on-disk skill directory (built-in/filesystem skills) into a zip byte stream.

    Same layout as ``build_skill_zip``; skips version-control/cache directories
    and junk files, and packs binary files as-is (no base64 round-trip,
    lossless).
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(skill_dir.rglob("*")):
            if not p.is_file() or p.name in JUNK_BASENAMES:
                continue
            rel = p.relative_to(skill_dir)
            if _ZIP_SKIP_DIR_PARTS.intersection(rel.parts):
                continue
            zf.write(p, f"{skill_id}/{rel.as_posix()}")
    return buf.getvalue()


def _upsert_admin_submission(
    db: Session,
    *,
    skill_id: str,
    skill_content: str,
    display_name: str,
    summary: str,
    category: str,
    tags: List[str],
    version: str,
    extra_files: Dict[str, str],
    note: str,
    submitter_name: str,
) -> Tuple[str, str]:
    """Upsert an approved listing record under the ADMIN_UPLOAD_OWNER sentinel, returning (slug, action).

    When the same skill_id already has an admin listing record, update it
    (keeping the original slug); otherwise create one. Shared by the two admin
    listing entry points: "upload zip" and "create via form".
    """
    category = _validate_category(category)
    snapshot_files = dict(extra_files)
    # Credential files are never distributed with the market snapshot (same as community listing).
    snapshot_files.pop(SECRETS_FILENAME, None)
    now = datetime.utcnow()
    existing = (
        db.query(MarketplaceSubmission)
        .filter(
            MarketplaceSubmission.skill_id == skill_id,
            MarketplaceSubmission.owner_user_id == ADMIN_UPLOAD_OWNER,
        )
        .first()
    )
    if existing is not None:
        existing.display_name = display_name
        existing.summary = summary
        existing.category = category
        existing.tags = list(tags or [])
        existing.version = version or "1.0.0"
        existing.skill_content = skill_content
        existing.extra_files = snapshot_files
        existing.note = note
        existing.status = "approved"
        existing.review_note = ""
        existing.reviewed_at = now
        existing.updated_at = now
        flag_modified(existing, "tags")
        flag_modified(existing, "extra_files")
        return existing.slug, "updated"

    slug = _derive_slug(db, skill_id, ADMIN_UPLOAD_OWNER)
    db.add(MarketplaceSubmission(
        submission_id=f"mkadm_{uuid.uuid4().hex[:16]}",
        slug=slug,
        skill_id=skill_id,
        owner_user_id=ADMIN_UPLOAD_OWNER,
        submitter_name=(submitter_name or "").strip(),
        display_name=display_name,
        summary=summary,
        category=category,
        tags=list(tags or []),
        version=version or "1.0.0",
        note=note,
        skill_content=skill_content,
        extra_files=snapshot_files,
        status="approved",
        reviewed_at=now,
        created_at=now,
        updated_at=now,
    ))
    return slug, "published"


def publish_skill_zip_to_marketplace(
    db: Session,
    data: bytes,
    *,
    category: str,
    display_name: str = "",
    summary: str = "",
    submitter_name: str = "管理员上传",
) -> Dict[str, Any]:
    """Admin uploads a skill zip → listed directly in the marketplace as approved (not in the catalog, not globally effective).

    When the same skill (by SKILL.md's name = skill_id) is uploaded again, the
    existing admin listing record is updated (keeping the original slug) rather
    than adding a new one each time.
    """
    parsed = parse_skill_zip(data)
    meta = parsed["meta"]
    name = (display_name or "").strip() or meta.name or parsed["skill_id"]
    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=name,
        description=(summary or "").strip() or meta.description or "",
        instructions=parsed["skill_content"],
        tool_names=list(meta.allowed_tools or []),
        ontology_tags=list(meta.tags or []),
    )
    slug, action = _upsert_admin_submission(
        db,
        skill_id=parsed["skill_id"],
        skill_content=parsed["skill_content"],
        display_name=name,
        summary=(summary or "").strip() or meta.description or "",
        category=category,
        tags=list(meta.tags or []),
        version=meta.version or "1.0.0",
        extra_files=parsed["extra_files"],
        note="管理员上传，免审核直接上架",
        submitter_name=submitter_name,
    )
    db.commit()
    logger.info("marketplace_skill_admin_%s: skill=%s slug=%s", action, parsed["skill_id"], slug)
    return {
        "slug": slug,
        "skill_id": parsed["skill_id"],
        "display_name": name,
        "category": _validate_category(category),
        "stored_files": len([k for k in parsed["extra_files"] if k != SECRETS_FILENAME]),
        "skipped": parsed["skipped"],
        "action": action,
        "message": "技能已上架技能市场" if action == "published" else "技能市场内容已更新",
    }


def publish_skill_to_marketplace(
    db: Session,
    *,
    skill_id: str,
    skill_content: str,
    display_name: str,
    description: str,
    version: str,
    tags: List[str],
    category: str,
    submitter_name: str = "管理员新建",
) -> Dict[str, Any]:
    """Admin "create skill" form → listed in the marketplace as approved (not in the catalog, not globally effective).

    ``skill_content`` is the complete SKILL.md assembled from the form fields
    (valid frontmatter).
    """
    name = (display_name or "").strip() or skill_id
    try:
        meta = _load_skill_metadata_from_str(skill_content, skill_id)
        allowed_tools = list(meta.allowed_tools or [])
        parsed_description = meta.description or ""
    except Exception:  # Some reviewed legacy drafts have minimal frontmatter.
        allowed_tools = []
        parsed_description = ""
    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=name,
        description=description or parsed_description,
        instructions=skill_content,
        tool_names=allowed_tools,
        ontology_tags=list(tags or []),
    )
    slug, action = _upsert_admin_submission(
        db,
        skill_id=skill_id,
        skill_content=skill_content,
        display_name=name,
        summary=(description or "").strip(),
        category=category,
        tags=list(tags or []),
        version=version or "1.0.0",
        extra_files={},
        note="管理员新建，免审核直接上架",
        submitter_name=submitter_name,
    )
    db.commit()
    logger.info("marketplace_skill_admin_%s: skill=%s slug=%s (form)", action, skill_id, slug)
    return {
        "slug": slug,
        "skill_id": skill_id,
        "display_name": name,
        "category": _validate_category(category),
        "action": action,
        "message": "技能已上架技能市场" if action == "published" else "技能市场内容已更新",
    }


def delete_marketplace_skill(db: Session, slug: str) -> Dict[str, Any]:
    """Admin deletes a DB listing record from the skill marketplace (admin upload / user community listing).

    Preloaded (filesystem) skills ship with the repo and cannot be deleted
    online — straight 400. Deletion only removes it from the market; it does not
    affect any installed skill instances (installs are content snapshots).
    """
    if _read_manifest(slug) is not None:
        raise BadRequestError(message="预置技能随仓库发布，不可在线删除")
    sub = (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.slug == slug)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("marketplace_skill", slug)
    skill_id, owner = sub.skill_id, sub.owner_user_id
    db.delete(sub)
    db.commit()
    logger.info("marketplace_skill_deleted: slug=%s skill=%s owner=%s", slug, skill_id, owner)
    return {"slug": slug, "deleted": True}


# ── Community listing: user submission + admin review ────────────────────────

def _existing_slugs(db: Session) -> set:
    """Currently occupied market slugs: the preloaded directory + all submissions.

    Must include rejected rows — the slug column is table-wide UNIQUE and
    rejected submissions still occupy their slug; if derivation skipped them,
    "re-submitting after rejection" would hit the unique constraint and 500
    outright.
    """
    slugs = set()
    if MARKETPLACE_DIR.is_dir():
        slugs.update(c.name for c in MARKETPLACE_DIR.iterdir() if c.is_dir())
    rows = db.query(MarketplaceSubmission.slug).all()
    slugs.update(r[0] for r in rows)
    return slugs


def _derive_slug(db: Session, skill_id: str, owner_user_id: str) -> str:
    """Derive the market slug from the source skill id: strip the user's own install-fingerprint suffix, then ensure global uniqueness."""
    base = skill_id
    suffix = f"-{_user_suffix(owner_user_id)}"
    if base.endswith(suffix):
        base = base[: -len(suffix)]
    base = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-") or "skill"
    taken = _existing_slugs(db)
    slug = base
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def _submission_to_dict(sub: MarketplaceSubmission, *, with_content: bool = False) -> Dict[str, Any]:
    data = {
        "submission_id": sub.submission_id,
        "slug": sub.slug,
        "skill_id": sub.skill_id,
        "owner_user_id": sub.owner_user_id,
        "submitter_name": sub.submitter_name or "",
        "display_name": sub.display_name,
        "summary": sub.summary or "",
        "category": sub.category or "社区共享",
        "tags": list(sub.tags or []),
        "version": sub.version or "1.0.0",
        "note": sub.note or "",
        "status": sub.status,
        "review_note": sub.review_note or "",
        "reviewed_at": sub.reviewed_at.isoformat() if sub.reviewed_at else None,
        "created_at": sub.created_at.isoformat() if sub.created_at else None,
        "file_count": len(sub.extra_files or {}),
    }
    if with_content:
        data["instructions"] = _strip_frontmatter(sub.skill_content or "")
        data["files"] = sorted((sub.extra_files or {}).keys())
    return data


def submit_to_marketplace(
    db: Session,
    skill_id: str,
    *,
    owner_user_id: str,
    submitter_name: str = "",
    note: str = "",
    category: str = "",
    summary: str = "",
) -> Dict[str, Any]:
    """User submits their private skill for marketplace listing (content snapshot, pending admin review)."""
    row = (
        db.query(AdminSkill)
        .filter(AdminSkill.skill_id == skill_id, AdminSkill.owner_user_id == owner_user_id)
        .first()
    )
    if row is None:
        raise ResourceNotFoundError("skill", skill_id)

    active = (
        db.query(MarketplaceSubmission)
        .filter(
            MarketplaceSubmission.skill_id == skill_id,
            MarketplaceSubmission.owner_user_id == owner_user_id,
            MarketplaceSubmission.status.in_(["pending", "approved"]),
        )
        .first()
    )
    if active is not None:
        state = "待审核" if active.status == "pending" else "已上架"
        raise HTTPException(status_code=409, detail=f"该技能已有{state}的上架申请")

    # The snapshot excludes the credentials file: when installing a market
    # skill with secrets, _inject_secrets writes the user's API key in
    # plaintext into extra_files[SECRETS_FILENAME] — that must never be
    # distributed to all installers with the listing snapshot. The credential
    # field names are kept with the snapshot as a sentinel entry (without
    # values), so installers are prompted to enter their own keys; SKILL.md is
    # correspondingly stripped of the injected section.
    snapshot_files = dict(row.extra_files or {})
    submitter_secrets = snapshot_files.pop(SECRETS_FILENAME, None)
    snapshot_content = row.skill_content
    if submitter_secrets:
        snapshot_content = _strip_injected_secrets(row.skill_content or "")
        try:
            secret_keys = sorted(json.loads(submitter_secrets).keys())
        except (TypeError, ValueError, AttributeError):
            secret_keys = []
        if secret_keys:
            # The original required/label metadata is unavailable in the installed artifact; approximate as "all required"
            snapshot_files[REQUIRED_SECRETS_SNAPSHOT] = json.dumps(
                [{"key": k, "label": k, "required": True} for k in secret_keys],
                ensure_ascii=False,
            )

    now = datetime.utcnow()
    sub = MarketplaceSubmission(
        submission_id=f"mksub_{uuid.uuid4().hex[:16]}",
        slug=_derive_slug(db, skill_id, owner_user_id),
        skill_id=skill_id,
        owner_user_id=owner_user_id,
        submitter_name=(submitter_name or "").strip(),
        display_name=row.display_name or skill_id,
        summary=(summary or "").strip() or (row.description or ""),
        category=_validate_category(category),
        tags=list(row.tags or []),
        version=row.version or "1.0.0",
        note=(note or "").strip(),
        skill_content=snapshot_content,
        extra_files=snapshot_files,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    db.add(sub)
    db.commit()
    logger.info(
        "marketplace_submission_created: id=%s skill=%s owner=%s slug=%s",
        sub.submission_id, skill_id, owner_user_id, sub.slug,
    )
    return _submission_to_dict(sub)


def list_my_submissions(db: Session, owner_user_id: str) -> List[Dict[str, Any]]:
    """All of the current user's listing submissions (newest → oldest)."""
    rows = (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.owner_user_id == owner_user_id)
        .order_by(MarketplaceSubmission.created_at.desc())
        .all()
    )
    return [_submission_to_dict(r) for r in rows]


def withdraw_submission(db: Session, submission_id: str, owner_user_id: str) -> None:
    """User withdraws their own submission: pending is deleted directly; approved requires an admin to delist; rejected can be deleted and resubmitted."""
    sub = (
        db.query(MarketplaceSubmission)
        .filter(
            MarketplaceSubmission.submission_id == submission_id,
            MarketplaceSubmission.owner_user_id == owner_user_id,
        )
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("marketplace_submission", submission_id)
    if sub.status == "approved":
        raise BadRequestError(message="技能已上架，如需下架请联系管理员")
    db.delete(sub)
    db.commit()


def list_submissions(db: Session, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Admin view: all listing submissions, filterable by status (pending first, newest → oldest)."""
    q = db.query(MarketplaceSubmission)
    if status:
        q = q.filter(MarketplaceSubmission.status == status)
    rows = q.order_by(MarketplaceSubmission.created_at.desc()).all()
    order = {"pending": 0, "approved": 1, "rejected": 2}
    rows.sort(key=lambda r: order.get(r.status, 3))
    return [_submission_to_dict(r) for r in rows]


def get_submission(db: Session, submission_id: str) -> Dict[str, Any]:
    """Submission detail for admin review (including a SKILL.md body preview and the attachment list)."""
    sub = (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.submission_id == submission_id)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("marketplace_submission", submission_id)
    return _submission_to_dict(sub, with_content=True)


def review_submission(
    db: Session,
    submission_id: str,
    *,
    approve: bool,
    review_note: str = "",
    category: str = "",
) -> Dict[str, Any]:
    """Admin review: approval means listing (appears in the market list); rejecting pending = decline, rejecting approved = delist."""
    sub = (
        db.query(MarketplaceSubmission)
        .filter(MarketplaceSubmission.submission_id == submission_id)
        .first()
    )
    if sub is None:
        raise ResourceNotFoundError("marketplace_submission", submission_id)
    # State-machine guard: approve only allows pending→approved (prevents two
    # concurrent admins / a stale list mis-click from listing a just-rejected
    # submission directly); reject allows pending (decline) and approved
    # (delist).
    if approve and sub.status != "pending":
        state = "已上架" if sub.status == "approved" else "已驳回"
        raise HTTPException(status_code=409, detail=f"该申请{state}，不能重复审核通过（如需重新上架请让用户重新提交）")
    if not approve and sub.status == "rejected":
        raise HTTPException(status_code=409, detail="该申请已是驳回状态")
    sub.status = "approved" if approve else "rejected"
    sub.review_note = (review_note or "").strip()
    if approve and (category or "").strip():
        sub.category = _validate_category(category)
    sub.reviewed_at = datetime.utcnow()
    sub.updated_at = datetime.utcnow()
    db.commit()
    logger.info(
        "marketplace_submission_reviewed: id=%s slug=%s status=%s",
        sub.submission_id, sub.slug, sub.status,
    )
    return _submission_to_dict(sub)
