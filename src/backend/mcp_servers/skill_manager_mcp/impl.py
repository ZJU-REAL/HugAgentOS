"""技能管理 MCP —— 业务实现（直连 DB / 复用后端 service，按 X-Current-User-Id 归属）。

复用 ``marketplace_service``（搜/装/上架）、``plugin_service``（URL 拉到的插件包导入）、
``core.artifacts.store``（读沙箱创作产物）。所有写操作强制按 ``user_id`` 归属，绝不跨用户。

落库通路（③⑤）：skill-creator 技能在沙箱里产出技能目录 → ``tar`` 打包 → 调框架自带的
``sandbox_get_artifact`` 推进**共享产物库**拿到 artifact_id → 本 MCP ``register_skill``
从共享产物库读出 tar、解包、校验、upsert ``AdminSkill``。MCP 容器与 backend 挂同一
``/app/storage`` 卷，故读得到产物库（但读不到沙箱本身）。
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# tar/zip 解包安全上限（防 zip-bomb / 超大产物）
_MAX_ENTRIES = 4000
_MAX_TOTAL_BYTES = 64 * 1024 * 1024  # 64 MB 解压后总量

# edit_skill 经工具入参写入单个附属文件的上限（智能体传的是 UTF-8 文本，非二进制大文件）
_MAX_SKILL_FILE_BYTES = 5 * 1024 * 1024  # 5 MB


# ── 通用 ────────────────────────────────────────────────────────────────
def _no_user() -> Dict[str, Any]:
    return {"ok": False, "message": "❌ 无法确定用户身份（缺 X-Current-User-Id 头），拒绝操作。"}


def _invalidate_user_cache(user_id: Optional[str]) -> None:
    """清掉该用户的 30s 能力解析缓存。owner 为 None（全局技能）时清全部。

    注意：本 MCP 跑在独立的 ``mcp`` 容器进程，能力缓存是**进程内**的——这里只清得掉本进程的，
    清不到 backend 进程（智能体真正读缓存的地方）。对智能体侧的**有效**失效由前端在技能变更后
    重新拉 ``GET /v1/catalog``（跑在 backend 进程）顺带完成，见 api/routes/v1/catalog.py。
    本调用作为进程内一致性的兜底保留，无害。"""
    try:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache(str(user_id) if user_id else None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("skill_manager: invalidate_capability_cache failed (%s)", exc)


def _require_cap(db, user_id: str, cap: str) -> Optional[Dict[str, Any]]:
    """能力位校验。缺权限返回错误 dict，否则 None。"""
    try:
        from core.auth.capabilities import resolve_user_capabilities

        if not resolve_user_capabilities(db, user_id).get(cap):
            return {
                "ok": False,
                "message": f"❌ 管理员未开放该能力（{cap}），无法执行。请联系管理员在权限设置里开启。",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill_manager: capability check failed (%s)", exc)
        return {"ok": False, "message": f"❌ 权限校验失败：{exc}"}
    return None


# ── SKILL.md 组装 / 校验（edit_skill 复用；镜像 api/routes/v1/admin_skills.py 同名逻辑，
#    但不跨层 import 那个重依赖 api.deps 的路由模块，故在本 MCP 进程内独立实现一份）───────
def _sanitize_fm_value(value: str) -> str:
    """frontmatter 单行字段：去掉换行，避免撑坏 YAML 结构。"""
    return (value or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()


def _build_skill_content(
    skill_id: str,
    display_name: str,
    description: str,
    version: str,
    tags: List[str],
    allowed_tools: List[str],
    instructions: str,
) -> str:
    """由结构化字段拼回一整份 SKILL.md（含 frontmatter + 正文）。"""
    fm_lines = [
        "---",
        f"name: {skill_id}",
        f"display_name: {_sanitize_fm_value(display_name)}",
        f"description: {_sanitize_fm_value(description)}",
        f"version: {_sanitize_fm_value(version)}",
    ]
    if tags:
        fm_lines.append(f"tags: {', '.join(tags)}")
    if allowed_tools:
        fm_lines.append(f"allowed_tools: {' '.join(allowed_tools)}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(instructions)
    fm_lines.append("")
    return "\n".join(fm_lines)


def _validate_skill_file_path(filename: str) -> str:
    """校验附属文件相对路径，拦目录穿越（extra_files 的 key 会在物化时拼进磁盘路径）。"""
    name = (filename or "").strip().strip("/")
    if not name:
        raise ValueError("文件名不能为空")
    if "\\" in name or "\x00" in name:
        raise ValueError(f"非法文件名：{filename}")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"非法文件路径：{filename}")
    return name


# ── ① 搜索技能市场（只读）────────────────────────────────────────────────
def search_marketplace(*, user_id: str, query: str = "", category: str = "") -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    from core.db.engine import SessionLocal
    from core.services import marketplace_service

    q = (query or "").strip().lower()
    cat = (category or "").strip()
    with SessionLocal() as db:
        # 与用户端市场接口同一套可见范围过滤（scoped 条目仅授权者可见）
        items = marketplace_service.list_marketplace_skills(db, viewer_user_id=user_id)

    def _match(it: Dict[str, Any]) -> bool:
        if cat and str(it.get("category") or "") != cat:
            return False
        if not q:
            return True
        hay = " ".join(
            str(it.get(k) or "")
            for k in ("slug", "display_name", "summary", "description", "category")
        )
        hay += " " + " ".join(str(t) for t in (it.get("tags") or []))
        return q in hay.lower()

    hits = [it for it in items if _match(it)]
    slim = [
        {
            "slug": it.get("slug"),
            "display_name": it.get("display_name"),
            "summary": it.get("summary") or it.get("description") or "",
            "category": it.get("category"),
            "tags": it.get("tags") or [],
            "installed": bool(it.get("installed")),
            "source": it.get("source"),
        }
        for it in hits
    ]
    msg = (
        f"技能市场匹配 {len(slim)} 个技能"
        + (f"（关键词「{query}」）" if q else "")
        + (f"（分类「{cat}」）" if cat else "")
        + "。想安装某个用 install_from_marketplace(slug)。"
    )
    return {"ok": True, "count": len(slim), "skills": slim, "message": msg}


# ── ② 从市场安装（私有，写）──────────────────────────────────────────────
def install_from_marketplace(
    *, user_id: str, slug: str, secrets: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "message": "❌ 请提供要安装的技能 slug。"}
    from core.db.engine import SessionLocal
    from core.services import marketplace_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_skill")
        if cap_err:
            return cap_err
        # 可见范围守卫：对该用户不可见的 scoped 条目按不存在处理（与用户端安装接口一致）
        from core.auth.marketplace_visibility import is_item_visible
        from core.services import marketplace_listing as ml
        if not is_item_visible(db, ml.KIND_SKILL, slug, user_id):
            return {"ok": False, "message": f"❌ 技能市场里找不到「{slug}」。"}
        try:
            res = marketplace_service.install_marketplace_skill(
                db, slug, owner_user_id=user_id, secrets=secrets or {}
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 安装失败：{exc}"}
    _invalidate_user_cache(user_id)
    return {
        "ok": True,
        "skill_id": res.get("id") or res.get("skill_id"),
        "action": res.get("action"),
        "message": f"✅ 已安装技能「{slug}」到你的私有技能库，可直接在对话中使用。",
    }


# ── ③⑤ 落库：从共享产物库读技能 tar，解包 → 校验 → upsert AdminSkill（写）──────
def register_skill(
    *, user_id: str, artifact_id: str, make_private: bool = True
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    artifact_id = (artifact_id or "").strip()
    if not artifact_id:
        return {"ok": False, "message": "❌ 缺少 artifact_id。请先在沙箱里把技能目录打成 tar 并调 sandbox_get_artifact 取得 artifact_id。"}

    data = _read_artifact_bytes(artifact_id)
    if data is None:
        return {"ok": False, "message": f"❌ 产物库里找不到 artifact_id「{artifact_id}」（或已过期）。"}

    from core.db.engine import SessionLocal

    with tempfile.TemporaryDirectory(prefix="skreg_") as tmp:
        tmp_path = Path(tmp)
        try:
            _safe_extract(data, tmp_path)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 解包失败：{exc}"}

        root = _locate_root(tmp_path)
        if root is None:
            return {
                "ok": False,
                "message": "❌ 包里既没有 SKILL.md 也没有 plugin.json，无法识别为技能或插件。",
            }

        # 插件包（含 plugin.json）→ 走插件导入链路
        if (root / "plugin.json").is_file() or (root / ".claude-plugin" / "plugin.json").is_file():
            with SessionLocal() as db:
                cap_err = _require_cap(db, user_id, "can_import_plugin")
                if cap_err:
                    return cap_err
                try:
                    from core.services import plugin_service

                    res = plugin_service.import_plugin(
                        db, root, owner_user_id=user_id, created_by="agent_skill_creator"
                    )
                except Exception as exc:  # noqa: BLE001
                    return {"ok": False, "message": f"❌ 插件导入失败：{exc}"}
            return {
                "ok": True,
                "kind": "plugin",
                "install_id": res.get("install_id"),
                "import_report": res.get("import_report"),
                "message": "✅ 已作为插件导入到你的私有空间。",
            }

        # 普通技能包（含 SKILL.md）→ upsert AdminSkill
        with SessionLocal() as db:
            cap_err = _require_cap(db, user_id, "can_add_skill")
            if cap_err:
                return cap_err
            try:
                if not make_private:
                    logger.warning(
                        "skill_manager: ignoring make_private=false for user %s",
                        user_id,
                    )
                return _register_skill_dir(db, root, owner_user_id=user_id)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "message": f"❌ 落库失败：{exc}"}


def _register_skill_dir(db, root: Path, *, owner_user_id: Optional[str]) -> Dict[str, Any]:
    """把一个含 SKILL.md 的目录 upsert 成 AdminSkill（镜像 marketplace install 的落库段）。"""
    from sqlalchemy.orm.attributes import flag_modified

    from core.agent_skills.deps_detector import detect_dependencies
    from core.agent_skills.binary_files import is_binary_value
    from core.agent_skills.cache_refresh import refresh_skill_caches
    from core.agent_skills.registry import _load_skill_metadata_from_str, _split_frontmatter
    from core.db.models import AdminSkill
    from core.services import marketplace_service as mk

    skill_content, extra_files = mk._load_package_files(root)

    # 读原始 frontmatter（不过 _require_id 校验，避免作者写了含空格/中文的 name 就直接抛）：
    # 取人类可读名做 display，取 slug 化后的名做命名空间化的 install_id。
    fm, _body = _split_frontmatter(skill_content)
    human_name = str(fm.get("display_name") or fm.get("name") or root.name).strip() or root.name
    base_name = _slugify(fm.get("name") or human_name)
    install_id = mk.compute_install_id(base_name, owner_user_id)
    skill_content = mk._rewrite_frontmatter_name(skill_content, install_id)

    # 改写 name 后再校验（description 必填等），确保落库内容合法。
    meta = _load_skill_metadata_from_str(skill_content, install_id)
    dependencies = detect_dependencies(
        {fn: c for fn, c in extra_files.items() if not is_binary_value(c)}
    )
    display_name = human_name or install_id
    description = meta.description or ""
    tags = list(meta.tags or [])
    version = meta.version or "1.0.0"

    now = datetime.utcnow()
    existing = db.query(AdminSkill).filter(AdminSkill.skill_id == install_id).first()
    if existing is not None:
        if existing.owner_user_id != owner_user_id:
            return {"ok": False, "message": f"❌ 技能 id「{install_id}」已被占用（公共技能或他人私有技能）。"}
        existing.skill_content = skill_content
        existing.display_name = display_name
        existing.description = description
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
        db.add(
            AdminSkill(
                skill_id=install_id,
                skill_content=skill_content,
                display_name=display_name,
                description=description,
                version=version,
                tags=tags,
                allowed_tools=list(meta.allowed_tools or []),
                extra_files=extra_files,
                dependencies=dependencies,
                is_enabled=True,
                owner_user_id=owner_user_id,
                created_at=now,
                updated_at=now,
                created_by="agent_skill_creator",
            )
        )
        action = "created"
    db.commit()
    refresh_skill_caches()
    _invalidate_user_cache(owner_user_id)
    scope = "私有" if owner_user_id else "全局"
    return {
        "ok": True,
        "kind": "skill",
        "skill_id": install_id,
        "action": action,
        "message": f"✅ 已{('更新' if action == 'updated' else '创建')}{scope}技能「{display_name}」（id={install_id}），可直接在对话中使用。要上架市场用 submit_to_marketplace。",
    }


# ── ④ 申请上架市场（写）──────────────────────────────────────────────────
def submit_to_marketplace(
    *,
    user_id: str,
    skill_id: str,
    category: str = "",
    summary: str = "",
    note: str = "",
    submitter_name: str = "",
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    skill_id = (skill_id or "").strip()
    if not skill_id:
        return {"ok": False, "message": "❌ 请提供要上架的私有技能 skill_id（先用 list_my_skills 查看）。"}
    from core.db.engine import SessionLocal
    from core.services import marketplace_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_skill")
        if cap_err:
            return cap_err
        try:
            res = marketplace_service.submit_to_marketplace(
                db,
                skill_id,
                owner_user_id=user_id,
                submitter_name=submitter_name or "智能体代提交",
                note=note,
                category=category,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 上架申请失败：{exc}"}
    return {
        "ok": True,
        "submission_id": res.get("submission_id") or res.get("id"),
        "status": res.get("status", "pending"),
        "message": f"✅ 已提交上架申请（技能 {skill_id}），进入管理员审核队列，审核通过后其他用户即可安装。",
    }


# ── 管理：列出我的技能 / 删除（写）──────────────────────────────────────────
def list_my_skills(*, user_id: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    from core.db.engine import SessionLocal
    from core.db.models import AdminSkill

    with SessionLocal() as db:
        rows = (
            db.query(AdminSkill)
            .filter(AdminSkill.owner_user_id == user_id)
            .order_by(AdminSkill.updated_at.desc())
            .all()
        )
        skills = [
            {
                "skill_id": r.skill_id,
                "display_name": r.display_name,
                "description": (r.description or "")[:160],
                "version": r.version,
                "is_enabled": bool(r.is_enabled),
                "dep_status": r.dep_status,
                "source_plugin": r.source_plugin,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    msg = f"你有 {len(skills)} 个私有技能。" if skills else "你还没有私有技能（可用 skill-creator 技能创建，或从市场安装）。"
    return {"ok": True, "count": len(skills), "skills": skills, "message": msg}


def delete_skill(*, user_id: str, skill_ref: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    skill_ref = (skill_ref or "").strip()
    if not skill_ref:
        return {"ok": False, "message": "❌ 请提供要删除的技能 skill_id 或名称。"}
    from core.db.engine import SessionLocal
    from core.db.models import AdminSkill

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_skill")
        if cap_err:
            return cap_err
        row, cands = _resolve_skill(db, user_id, skill_ref)
        if row is None and cands:
            return {
                "ok": False,
                "need_clarification": True,
                "message": "匹配到多个技能，请用 skill_id 指明：",
                "candidates": [{"skill_id": c.skill_id, "display_name": c.display_name} for c in cands],
            }
        if row is None:
            return {"ok": False, "message": f"❌ 没找到你的技能「{skill_ref}」。"}
        deleted_id = row.skill_id
        display = row.display_name
        try:
            from core.services.skill_icon_service import delete_skill_icon

            delete_skill_icon(db, deleted_id)
        except Exception:  # noqa: BLE001
            pass
        db.delete(row)
        db.commit()
        try:
            from core.agent_skills.cache_refresh import refresh_skill_caches

            refresh_skill_caches()
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill_manager delete: cache refresh failed (%s)", exc)
    _invalidate_user_cache(user_id)
    return {"ok": True, "skill_id": deleted_id, "message": f"✅ 已删除私有技能「{display}」（id={deleted_id}）。"}


# ── 编辑：原地修改我的私有技能（元数据 / 正文 / 附属文件，字段级部分更新，写）──────────────
def edit_skill(
    *,
    user_id: str,
    skill_ref: str,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    tags: Optional[List[str]] = None,
    version: Optional[str] = None,
    files_upsert: Optional[Dict[str, str]] = None,
    files_delete: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """修改一个本人私有技能：只更新传入的字段，未传的保持原样。skill_id 不可改。"""
    if not user_id:
        return _no_user()
    skill_ref = (skill_ref or "").strip()
    if not skill_ref:
        return {"ok": False, "message": "❌ 请提供要编辑的技能 skill_id 或名称。"}

    files_upsert = files_upsert or {}
    files_delete = files_delete or []
    nothing = (
        display_name is None
        and description is None
        and instructions is None
        and tags is None
        and version is None
        and not files_upsert
        and not files_delete
    )
    if nothing:
        return {
            "ok": False,
            "message": "❌ 没有指定任何要修改的内容。请给出 display_name/description/instructions/tags/version 里的一项或多项，或 files_upsert/files_delete。",
        }

    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_skill")
        if cap_err:
            return cap_err

        row, cands = _resolve_skill(db, user_id, skill_ref)
        if row is None and cands:
            return {
                "ok": False,
                "need_clarification": True,
                "message": "匹配到多个技能，请用 skill_id 指明要编辑哪一个：",
                "candidates": [{"skill_id": c.skill_id, "display_name": c.display_name} for c in cands],
            }
        if row is None:
            return {"ok": False, "message": f"❌ 没找到你的技能「{skill_ref}」（只能编辑本人的私有技能）。"}

        from sqlalchemy.orm.attributes import flag_modified

        from core.agent_skills.binary_files import is_binary_value
        from core.agent_skills.cache_refresh import refresh_skill_caches
        from core.agent_skills.deps_detector import detect_dependencies
        from core.agent_skills.registry import _load_skill_metadata_from_str, _split_frontmatter

        changed: List[str] = []

        # 基线取自技能行现有值；正文从 skill_content 剥 frontmatter 得到。
        new_display = row.display_name
        if display_name is not None:
            v = _sanitize_fm_value(display_name)
            if not v:
                return {"ok": False, "message": "❌ display_name 不能为空。"}
            new_display = v
            changed.append("名称")

        new_desc = row.description or ""
        if description is not None:
            v = (description or "").strip()
            if not v:
                return {"ok": False, "message": "❌ description 不能为空（技能加载硬性必填）。"}
            new_desc = v
            changed.append("描述")

        try:
            _, cur_body = _split_frontmatter(row.skill_content or "")
        except Exception:
            cur_body = ""
        new_instructions = (cur_body or "").strip()
        if instructions is not None:
            v = (instructions or "").strip()
            if not v:
                return {"ok": False, "message": "❌ instructions（技能正文）不能为空。"}
            new_instructions = v
            changed.append("正文")

        new_version = row.version or "1.0.0"
        if version is not None:
            new_version = _sanitize_fm_value(version) or new_version
            changed.append("版本")

        new_tags = list(row.tags or [])
        if tags is not None:
            new_tags = [str(t).strip() for t in tags if str(t).strip()]
            changed.append("标签")

        allowed_tools = list(row.allowed_tools or [])  # 本工具不改工具白名单，原样保留

        skill_content = _build_skill_content(
            row.skill_id, new_display, new_desc, new_version, new_tags, allowed_tools, new_instructions
        )
        # 组装后过一遍注册校验（description 必填等），坏内容不落库。
        try:
            _load_skill_metadata_from_str(skill_content, row.skill_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 技能内容校验失败：{exc}"}

        # 附属文件：先删后增改；文件名过路径穿越校验，SKILL.md 走正文不走这里。
        extra = dict(row.extra_files or {})
        files_touched = False
        for fn in files_delete:
            try:
                name = _validate_skill_file_path(fn)
            except ValueError as exc:
                return {"ok": False, "message": f"❌ {exc}"}
            if name == "SKILL.md":
                return {"ok": False, "message": "❌ SKILL.md 是技能正文，请用 instructions 改，不要走 files_delete。"}
            if name in extra:
                del extra[name]
                files_touched = True
        for fn, content in files_upsert.items():
            try:
                name = _validate_skill_file_path(fn)
            except ValueError as exc:
                return {"ok": False, "message": f"❌ {exc}"}
            if name == "SKILL.md":
                return {"ok": False, "message": "❌ SKILL.md 是技能正文，请用 instructions 改，不要走 files_upsert。"}
            text = content if isinstance(content, str) else str(content)
            if len(text.encode("utf-8")) > _MAX_SKILL_FILE_BYTES:
                return {"ok": False, "message": f"❌ 文件「{name}」过大（上限 {_MAX_SKILL_FILE_BYTES // (1024 * 1024)}MB，二进制大文件请用 register_skill 打包上传）。"}
            extra[name] = text
            files_touched = True
        if files_touched:
            changed.append("附属文件")

        now = datetime.utcnow()
        row.skill_content = skill_content
        row.display_name = new_display
        row.description = new_desc
        row.version = new_version
        row.tags = new_tags
        row.updated_at = now
        flag_modified(row, "tags")
        if files_touched:
            row.extra_files = extra
            # 文件变了 → 重算依赖（仅非二进制文本参与探测），与 register 落库口径一致。
            row.dependencies = detect_dependencies(
                {fn: c for fn, c in extra.items() if not is_binary_value(c)}
            )
            flag_modified(row, "extra_files")
            flag_modified(row, "dependencies")

        edited_id = row.skill_id
        edited_name = new_display
        db.commit()

        try:
            refresh_skill_caches()
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill_manager edit: cache refresh failed (%s)", exc)

    _invalidate_user_cache(user_id)
    return {
        "ok": True,
        "skill_id": edited_id,
        "changed": changed,
        "message": f"✅ 已更新私有技能「{edited_name}」（id={edited_id}）：{('、'.join(changed) or '无字段变化')}。",
    }


def _resolve_skill(db, user_id: str, ref: str) -> Tuple[Optional[Any], List[Any]]:
    """skill_ref → (唯一命中, 候选)。先精确 skill_id，再按 display_name 模糊匹配。仅本人私有技能。"""
    from core.db.models import AdminSkill

    exact = (
        db.query(AdminSkill)
        .filter(AdminSkill.skill_id == ref, AdminSkill.owner_user_id == user_id)
        .first()
    )
    if exact:
        return exact, []
    cands = (
        db.query(AdminSkill)
        .filter(
            AdminSkill.owner_user_id == user_id,
            AdminSkill.display_name.ilike(f"%{ref}%"),
        )
        .order_by(AdminSkill.updated_at.desc())
        .limit(10)
        .all()
    )
    if len(cands) == 1:
        return cands[0], []
    return None, cands


# ── 产物库 / 解包 辅助 ────────────────────────────────────────────────────
def _read_artifact_bytes(file_id: str) -> Optional[bytes]:
    """从共享产物库按 file_id 读回字节（local: 读 path；oss: download_bytes）。"""
    try:
        from core.artifacts import store

        meta = store.get_artifact(file_id)
        if not meta:
            return None
        path = meta.get("path")
        if path and os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
        key = meta.get("storage_key")
        if key:
            from core.storage import get_storage

            return bytes(get_storage().download_bytes(key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill_manager: read artifact %s failed (%s)", file_id, exc)
    return None


def _safe_extract(data: bytes, dest: Path) -> None:
    """安全解包 tar(.gz) 或 zip 到 dest：拦目录穿越 + 限条目数/总量。"""
    if _looks_like_zip(data):
        _safe_extract_zip(data, dest)
    else:
        _safe_extract_tar(data, dest)


def _looks_like_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def _within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _safe_extract_tar(data: bytes, dest: Path) -> None:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        members = tf.getmembers()
        if len(members) > _MAX_ENTRIES:
            raise ValueError(f"包内条目过多（{len(members)} > {_MAX_ENTRIES}）")
        for m in members:
            if not (m.isfile() or m.isdir()):
                continue  # 跳过软链接/设备等，杜绝越权
            target = dest / m.name
            if not _within(dest, target):
                raise ValueError(f"检测到目录穿越条目：{m.name}")
            total += max(m.size, 0)
            if total > _MAX_TOTAL_BYTES:
                raise ValueError("解压后总量超限（>64MB）")
        tf.extractall(dest, members=[m for m in members if m.isfile() or m.isdir()])


def _safe_extract_zip(data: bytes, dest: Path) -> None:
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ENTRIES:
            raise ValueError(f"包内条目过多（{len(infos)} > {_MAX_ENTRIES}）")
        for info in infos:
            target = dest / info.filename
            if not _within(dest, target):
                raise ValueError(f"检测到目录穿越条目：{info.filename}")
            total += info.file_size
            if total > _MAX_TOTAL_BYTES:
                raise ValueError("解压后总量超限（>64MB）")
        zf.extractall(dest)


def _locate_root(extracted: Path) -> Optional[Path]:
    """定位技能/插件根：含 SKILL.md 或 plugin.json 的目录。tar 常多包一层。"""
    def _is_root(d: Path) -> bool:
        return (
            (d / "SKILL.md").is_file()
            or (d / "plugin.json").is_file()
            or (d / ".claude-plugin" / "plugin.json").is_file()
        )

    if _is_root(extracted):
        return extracted
    subdirs = [p for p in extracted.iterdir() if p.is_dir()]
    for d in subdirs:
        if _is_root(d):
            return d
    # 再下探一层
    for d in subdirs:
        for dd in (p for p in d.iterdir() if p.is_dir()):
            if _is_root(dd):
                return dd
    return None


def _slugify(name: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9_-]+", "-", (name or "").strip().lower()).strip("-")
    return (s or "skill")[:63]
