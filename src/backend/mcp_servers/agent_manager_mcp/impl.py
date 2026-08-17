"""智能体管理 MCP —— 业务实现（直连 DB / 复用后端 service，按 X-Current-User-Id 归属）。

复用 ``UserAgentService``（建/改/删/列）与 ``agent_market_service``（搜/装/上架）。
所有写操作强制 ``owner_type="user"`` + 按 ``user_id`` 归属，绝不落成 admin/team 智能体——
与 skill-manager 强制 ``make_private=True`` 是同一条原则：自助入口只能产出仅自己可见的资产。

子智能体建好后**下一轮对话即可用**：``orchestration/workflow.py`` 每轮启动时用
``UserAgentService.list_for_user`` 解析可见子智能体交给 ``register_subagent_tool``，
无需重启也无需额外注册。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 平台内置角色（探索员/执行员/审查员）不是 UserAgent 行，前缀固定；"我的智能体"要滤掉它们。
_BUILTIN_PREFIX = "builtin."

# system_prompt 上限：这是智能体的行事准则正文，给足空间但别让单条记录失控。
_MAX_PROMPT_BYTES = 200 * 1024

# 绑定类字段一次最多绑多少项，防止模型把整库 id 一股脑塞进来。
_MAX_BINDINGS = 100


# ── 通用 ────────────────────────────────────────────────────────────────
def _no_user() -> Dict[str, Any]:
    return {"ok": False, "message": "❌ 无法确定用户身份（缺 X-Current-User-Id 头），拒绝操作。"}


def _invalidate_user_cache(user_id: Optional[str]) -> None:
    """清掉该用户的 30s 能力解析缓存。

    注意：本 MCP 跑在独立的 ``mcp`` 容器进程，能力缓存是**进程内**的——这里只清得掉本进程的，
    清不到 backend 进程（智能体真正读缓存的地方）。对智能体侧的**有效**失效由前端在变更后
    重新拉 ``GET /v1/catalog``（跑在 backend 进程）顺带完成，见 hooks/chatStream.ts 的
    CATALOG_MUTATING_TOOLS 白名单。本调用作为进程内一致性的兜底保留，无害。
    """
    try:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache(str(user_id) if user_id else None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_manager: invalidate_capability_cache failed (%s)", exc)


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
        logger.warning("agent_manager: capability check failed (%s)", exc)
        return {"ok": False, "message": f"❌ 权限校验失败：{exc}"}
    return None


def _svc(db):
    from core.services.user_agent_service import UserAgentService

    return UserAgentService(db)


def valid_categories() -> List[str]:
    """子智能体市场的固定分类（**与技能市场的 8 类不同**）。

    从单一真相源读，别在工具描述里硬写一份——分类调整时两边会漂移，
    模型照着过期的列表猜，每次上架都要失败一轮。
    """
    from core.services.agent_market_categories import AGENT_MARKETPLACE_CATEGORIES

    return list(AGENT_MARKETPLACE_CATEGORIES)


def _clean_ids(raw: Optional[List[str]], field: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """绑定 id 数组归一化：去空白/去重/保序/限量。返回 (清洗后, 错误信息)。"""
    if raw is None:
        return None, None
    out: List[str] = []
    seen = set()
    for item in raw:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    if len(out) > _MAX_BINDINGS:
        return None, f"❌ {field} 一次最多绑 {_MAX_BINDINGS} 项（收到 {len(out)} 项）。"
    return out, None


def _binding_updates(
    raw: Dict[str, Optional[List[str]]], *, fill_missing: bool
) -> Tuple[Dict[str, List[str]], Optional[str]]:
    """把四个绑定字段一起清洗成待写入的 data 片段。

    create 与 edit 的唯一差别就是没传的字段怎么办：create 落空数组，edit 保持原样
    （即不进 data）。除此之外去重、限量规则完全相同，不该各写一遍。
    """
    data: Dict[str, List[str]] = {}
    for field, value in raw.items():
        if value is None and not fill_missing:
            continue
        cleaned, err = _clean_ids(value, field)
        if err:
            return {}, err
        data[field] = cleaned or []
    return data, None


def _clean_max_iters(value: Optional[int]) -> Tuple[Optional[int], Optional[str]]:
    """max_iters 校验。None 表示"没传"，返回 (None, None) 让调用方跳过。"""
    if value is None:
        return None, None
    try:
        mi = int(value)
    except (TypeError, ValueError):
        return None, "❌ max_iters 必须是整数。"
    if not 1 <= mi <= 100:
        return None, "❌ max_iters 取值范围 1–100。"
    return mi, None


def _slim(a: Dict[str, Any]) -> Dict[str, Any]:
    """把 service 的完整序列化裁成智能体读得动的摘要（别把 change_history 等整包回吐）。"""
    return {
        "agent_id": a.get("agent_id"),
        "name": a.get("name"),
        "description": a.get("description") or "",
        "is_enabled": bool(a.get("is_enabled")),
        "max_iters": a.get("max_iters"),
        "version": a.get("version"),
        "bindings": {
            "skills": len(a.get("skill_ids") or []),
            "mcp_servers": len(a.get("mcp_server_ids") or []),
            "plugins": len(a.get("plugin_ids") or []),
            "kb_spaces": len(a.get("kb_ids") or []),
        },
        "from_market": a.get("source_market_slug") or "",
        "updated_at": a.get("updated_at"),
    }


def _my_agents(db, user_id: str) -> List[Dict[str, Any]]:
    """只取"我自己建的/装的"子智能体。

    ``list_for_user`` 返回的是**可见集**（含管理员全局智能体与团队智能体），
    自助管理入口只能碰自己的，所以这里按 owner_type/user_id 再收一次口。
    """
    rows = _svc(db).list_for_user(user_id) or []
    return [
        a
        for a in rows
        if a.get("owner_type") == "user"
        and str(a.get("user_id") or "") == str(user_id)
        and not str(a.get("agent_id") or "").startswith(_BUILTIN_PREFIX)
    ]


def _resolve_agent(db, user_id: str, ref: str) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """agent_ref → (唯一命中, 候选)。先精确 agent_id，再按名称模糊匹配。仅本人的子智能体。"""
    ref = (ref or "").strip()
    mine = _my_agents(db, user_id)
    for a in mine:
        if str(a.get("agent_id")) == ref:
            return a, []
    low = ref.lower()
    cands = [a for a in mine if low and low in str(a.get("name") or "").lower()]
    if len(cands) == 1:
        return cands[0], []
    return None, cands[:10]


def _need_clarify(cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "ok": False,
        "need_clarification": True,
        "message": "匹配到多个子智能体，请用 agent_id 指明具体是哪一个：",
        "candidates": [{"agent_id": c.get("agent_id"), "name": c.get("name")} for c in cands],
    }


# ── ① 搜索子智能体市场（只读）──────────────────────────────────────────
def search_agent_market(*, user_id: str, query: str = "", category: str = "") -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    from core.db.engine import SessionLocal
    from core.services import agent_market_service

    q = (query or "").strip().lower()
    cat = (category or "").strip()
    with SessionLocal() as db:
        # 与用户端市场接口同一套可见范围过滤（scoped 条目仅授权者可见）
        items = agent_market_service.list_marketplace_agents(db, viewer_user_id=user_id)
        # list_marketplace_agents 只标 market_enabled/visibility，不标 installed——不补这一下，
        # 每条都会显示"未安装"，模型就会去重装用户已经有的智能体（一次代价高昂的克隆）。
        # 插件侧的 list_plugins 自带该标注，两边口径要一致。
        agent_market_service.annotate_installed(items, db, user_id)

    def _match(it: Dict[str, Any]) -> bool:
        if cat and str(it.get("category") or "") != cat:
            return False
        if not q:
            return True
        hay = " ".join(
            str(it.get(k) or "") for k in ("slug", "name", "summary", "description", "category")
        )
        hay += " " + " ".join(str(t) for t in (it.get("tags") or []))
        return q in hay.lower()

    hits = [it for it in items if _match(it)]
    slim = [
        {
            "slug": it.get("slug"),
            "name": it.get("name"),
            "summary": it.get("summary") or it.get("description") or "",
            "category": it.get("category"),
            "installed": bool(it.get("installed")),
        }
        for it in hits
    ]
    msg = (
        f"子智能体市场匹配 {len(slim)} 个"
        + (f"（关键词「{query}」）" if q else "")
        + (f"（分类「{cat}」）" if cat else "")
        + "。想安装某个用 install_market_agent(slug)。"
    )
    return {"ok": True, "count": len(slim), "agents": slim, "message": msg}


# ── ② 从市场安装（私有，写）────────────────────────────────────────────
def install_market_agent(*, user_id: str, slug: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    slug = (slug or "").strip()
    if not slug:
        return {"ok": False, "message": "❌ 请提供要安装的子智能体 slug。"}
    from core.db.engine import SessionLocal
    from core.services import agent_market_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_agent")
        if cap_err:
            return cap_err
        # 可见范围守卫：对该用户不可见的 scoped 条目按不存在处理（与用户端安装接口一致）。
        # 不要包 try——守卫出错就该报错，静默跳过等于对不可见条目失败开放。
        from core.auth.marketplace_visibility import is_item_visible
        from core.services import marketplace_listing as ml

        if not is_item_visible(db, ml.KIND_AGENT, slug, user_id):
            return {"ok": False, "message": f"❌ 子智能体市场里找不到「{slug}」。"}
        try:
            res = agent_market_service.install_marketplace_agent(
                db, slug, owner_user_id=user_id, operator_name=user_id
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 安装失败：{exc}"}
    _invalidate_user_cache(user_id)
    agent_id = (res or {}).get("agent_id")
    # install_report.dropped = 你账下没有的能力，装完绑不上——必须如实回报，别让用户以为全绑上了。
    report = (res or {}).get("install_report") or {}
    dropped = list(report.get("dropped") or [])
    tail = (
        f"注意有 {len(dropped)} 项能力你账下没有、已跳过：{'、'.join(str(d) for d in dropped[:5])}。"
        if dropped
        else ""
    )
    return {
        "ok": True,
        "agent_id": agent_id,
        "install_report": report,
        "message": (
            f"✅ 已从市场安装子智能体「{slug}」到你的智能体库"
            + (f"（agent_id={agent_id}）" if agent_id else "")
            + "，下一轮对话即可直接指派任务给它。"
            + tail
        ),
    }


# ── ③ 列出可绑能力（只读，创建/修改前必调）──────────────────────────────
def list_bindable_capabilities(*, user_id: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        try:
            res = _svc(db).list_available_resources(owner_user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 读取可绑能力失败：{exc}"}

    def _pick(items: Any) -> List[Dict[str, Any]]:
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            out.append(
                {
                    "id": it.get("id"),
                    "name": it.get("name") or it.get("id"),
                    "description": (str(it.get("description") or ""))[:160],
                }
            )
        return out

    skills = _pick(res.get("skills"))
    mcps = _pick(res.get("mcp_servers"))
    plugins = _pick(res.get("plugins"))
    kbs = _pick(res.get("kb_spaces"))
    return {
        "ok": True,
        "skills": skills,
        "mcp_servers": mcps,
        "plugins": plugins,
        "kb_spaces": kbs,
        "message": (
            f"可绑能力：技能 {len(skills)} 个、工具 {len(mcps)} 个、"
            f"插件 {len(plugins)} 个、知识库 {len(kbs)} 个。"
            "创建/修改子智能体时 id 必须取自这里，不要自行编造。"
        ),
    }


# ── ④ 从零创建（写）────────────────────────────────────────────────────
def create_agent(
    *,
    user_id: str,
    name: str,
    description: str = "",
    system_prompt: str = "",
    skill_ids: Optional[List[str]] = None,
    mcp_server_ids: Optional[List[str]] = None,
    plugin_ids: Optional[List[str]] = None,
    kb_ids: Optional[List[str]] = None,
    max_iters: Optional[int] = None,
    welcome_message: str = "",
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    name = (name or "").strip()
    if not name:
        return {"ok": False, "message": "❌ 请提供子智能体的名字（name）。"}
    description = (description or "").strip()
    if not description:
        return {"ok": False, "message": "❌ 请提供 description：一句话说清这个智能体管什么。"}
    system_prompt = system_prompt or ""
    if not system_prompt.strip():
        return {
            "ok": False,
            "message": "❌ 请提供 system_prompt（这个智能体的行事准则，是它的主体）。写法见 agent-designer 技能。",
        }
    if len(system_prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        return {"ok": False, "message": f"❌ system_prompt 过长（上限 {_MAX_PROMPT_BYTES // 1024}KB）。"}

    data: Dict[str, Any] = {
        "name": name,
        "description": description,
        "system_prompt": system_prompt,
        "welcome_message": (welcome_message or "").strip(),
    }
    bindings, err = _binding_updates(
        {
            "skill_ids": skill_ids,
            "mcp_server_ids": mcp_server_ids,
            "plugin_ids": plugin_ids,
            "kb_ids": kb_ids,
        },
        fill_missing=True,
    )
    if err:
        return {"ok": False, "message": err}
    data.update(bindings)

    mi, err = _clean_max_iters(max_iters)
    if err:
        return {"ok": False, "message": err}
    if mi is not None:
        data["max_iters"] = mi

    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_agent")
        if cap_err:
            return cap_err
        try:
            # 自助入口固定落成 owner_type="user"：只有自己可见可用，绝不落 admin/team。
            created = _svc(db).create(
                user_id=user_id,
                operator_name=user_id,
                owner_type="user",
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": _friendly_error("创建", exc)}
    _invalidate_user_cache(user_id)
    return {
        "ok": True,
        "agent": _slim(created),
        "message": (
            f"✅ 已创建子智能体「{name}」（agent_id={created.get('agent_id')}），"
            "仅你自己可见可用，下一轮对话即可直接指派任务给它。"
        ),
    }


def _friendly_error(action: str, exc: Exception) -> str:
    """把 service 层异常转成人话。本体校验失败要说清楚是本体没过，而不是甩一句失败。"""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "ontology" in low or "本体" in text:
        return f"❌ {action}失败：没通过本体校验——{text}。请按提示补齐名称/描述/正文或本体标签后重试。"
    if isinstance(exc, PermissionError):
        return f"❌ {action}失败：只能操作你自己的子智能体。"
    if isinstance(exc, LookupError):
        return f"❌ {action}失败：找不到该子智能体（可能已被删除）。"
    return f"❌ {action}失败：{text}"


# ── ⑤ 列出我的子智能体（只读）──────────────────────────────────────────
def list_my_agents(*, user_id: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        mine = _my_agents(db, user_id)
    agents = [_slim(a) for a in mine]
    msg = (
        f"你有 {len(agents)} 个自己的子智能体。"
        if agents
        else "你还没有自己的子智能体（可用 create_agent 新建，或用 search_agent_market 从市场装一个）。"
    )
    return {"ok": True, "count": len(agents), "agents": agents, "message": msg}


# ── ⑥ 修改（字段级部分更新，写）────────────────────────────────────────
def edit_agent(
    *,
    user_id: str,
    agent_ref: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    system_prompt: Optional[str] = None,
    skill_ids: Optional[List[str]] = None,
    mcp_server_ids: Optional[List[str]] = None,
    plugin_ids: Optional[List[str]] = None,
    kb_ids: Optional[List[str]] = None,
    max_iters: Optional[int] = None,
    is_enabled: Optional[bool] = None,
    welcome_message: Optional[str] = None,
) -> Dict[str, Any]:
    """修改一个本人的子智能体：只更新传入的字段，未传的保持原样。agent_id 不可改。"""
    if not user_id:
        return _no_user()
    agent_ref = (agent_ref or "").strip()
    if not agent_ref:
        return {"ok": False, "message": "❌ 请提供要修改的子智能体 agent_id 或名字。"}

    data: Dict[str, Any] = {}
    if name is not None:
        v = name.strip()
        if not v:
            return {"ok": False, "message": "❌ name 不能改成空。"}
        data["name"] = v
    if description is not None:
        v = description.strip()
        if not v:
            return {"ok": False, "message": "❌ description 不能改成空（它是这个智能体的职责说明）。"}
        data["description"] = v
    if system_prompt is not None:
        if not system_prompt.strip():
            return {"ok": False, "message": "❌ system_prompt 不能改成空（它是这个智能体的主体）。"}
        if len(system_prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            return {"ok": False, "message": f"❌ system_prompt 过长（上限 {_MAX_PROMPT_BYTES // 1024}KB）。"}
        data["system_prompt"] = system_prompt
    if welcome_message is not None:
        data["welcome_message"] = welcome_message.strip()
    bindings, err = _binding_updates(
        {
            "skill_ids": skill_ids,
            "mcp_server_ids": mcp_server_ids,
            "plugin_ids": plugin_ids,
            "kb_ids": kb_ids,
        },
        fill_missing=False,
    )
    if err:
        return {"ok": False, "message": err}
    data.update(bindings)

    mi, err = _clean_max_iters(max_iters)
    if err:
        return {"ok": False, "message": err}
    if mi is not None:
        data["max_iters"] = mi
    if is_enabled is not None:
        data["is_enabled"] = bool(is_enabled)

    if not data:
        return {
            "ok": False,
            "message": "❌ 没有要改的内容。请至少传一个字段（name/description/system_prompt/skill_ids/mcp_server_ids/plugin_ids/kb_ids/max_iters/is_enabled）。",
        }

    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_agent")
        if cap_err:
            return cap_err
        hit, cands = _resolve_agent(db, user_id, agent_ref)
        if hit is None and cands:
            return _need_clarify(cands)
        if hit is None:
            return {"ok": False, "message": f"❌ 没找到你的子智能体「{agent_ref}」。"}
        try:
            updated = _svc(db).update(
                agent_id=str(hit.get("agent_id")),
                user_id=user_id,
                operator_name=user_id,
                owner_type="user",
                data=data,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": _friendly_error("修改", exc)}
    _invalidate_user_cache(user_id)
    return {
        "ok": True,
        "agent": _slim(updated),
        "changed": sorted(data.keys()),
        "message": (
            f"✅ 已更新子智能体「{updated.get('name')}」（agent_id={updated.get('agent_id')}）："
            f"{'、'.join(sorted(data.keys()))}。"
        ),
    }


# ── ⑦ 删除（写）────────────────────────────────────────────────────────
def delete_agent(*, user_id: str, agent_ref: str) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    agent_ref = (agent_ref or "").strip()
    if not agent_ref:
        return {"ok": False, "message": "❌ 请提供要删除的子智能体 agent_id 或名字。"}
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_agent")
        if cap_err:
            return cap_err
        hit, cands = _resolve_agent(db, user_id, agent_ref)
        if hit is None and cands:
            return _need_clarify(cands)
        if hit is None:
            return {"ok": False, "message": f"❌ 没找到你的子智能体「{agent_ref}」。"}
        deleted_id = str(hit.get("agent_id"))
        display = hit.get("name")
        try:
            _svc(db).delete(agent_id=deleted_id, user_id=user_id, owner_type="user")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": _friendly_error("删除", exc)}
    _invalidate_user_cache(user_id)
    return {
        "ok": True,
        "agent_id": deleted_id,
        "message": f"✅ 已删除子智能体「{display}」（agent_id={deleted_id}）。",
    }


# ── ⑧ 申请上架市场（写）────────────────────────────────────────────────
def submit_agent_to_market(
    *, user_id: str, agent_id: str, category: str = "", summary: str = "", note: str = ""
) -> Dict[str, Any]:
    if not user_id:
        return _no_user()
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return {"ok": False, "message": "❌ 请提供要上架的 agent_id（取自 list_my_agents）。"}
    from core.db.engine import SessionLocal
    from core.services import agent_market_service

    with SessionLocal() as db:
        cap_err = _require_cap(db, user_id, "can_add_agent")
        if cap_err:
            return cap_err
        hit, cands = _resolve_agent(db, user_id, agent_id)
        if hit is None and cands:
            return _need_clarify(cands)
        if hit is None:
            return {"ok": False, "message": f"❌ 没找到你的子智能体「{agent_id}」。"}
        # 分类是固定集合（与技能市场的 8 类不同），传错会被 service 拒。先在这里挡一道，
        # 并把合法值回给模型，省得它反复猜。
        cat = (category or "").strip()
        cats = valid_categories()
        if cat and cat not in cats:
            return {
                "ok": False,
                "valid_categories": cats,
                "message": (
                    f"❌ 分类「{cat}」不合法。子智能体市场只接受这 {len(cats)} 个分类，"
                    "请挑最贴切的一个：" + "、".join(cats)
                ),
            }
        try:
            sub = agent_market_service.submit_to_marketplace(
                db,
                str(hit.get("agent_id")),
                owner_user_id=user_id,
                submitter_name=user_id,
                note=(note or "").strip(),
                category=cat,
                summary=(summary or "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"❌ 上架申请失败：{exc}"}
    return {
        "ok": True,
        "submission_id": (sub or {}).get("submission_id") or (sub or {}).get("id"),
        "status": (sub or {}).get("status") or "pending",
        "message": (
            f"✅ 已提交上架申请：子智能体「{hit.get('name')}」已进入管理员审核队列。"
            "这是申请不是直接上架，通过审核后其他人才能安装。"
        ),
    }
