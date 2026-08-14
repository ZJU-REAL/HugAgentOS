"""对话模式服务。

职责三块：

1. **解析**：给一段对话解析出它的装配契约（``resolve``）。运行时唯一真源——
   ``agent_factory`` 不再自己去读 ``turbo.*`` 配置键。
2. **CRUD**：官方模式（Config 台）与私有模式（用户设置页）走同一套增删改，
   只是可见范围和提示词落点不同。
3. **播种/迁移**：首次启动时把内置的「标准 / 极速」两行写进去，并把历史
   ``turbo.*`` SystemConfig 键**一次性**搬到极速那一行上。

可见范围规则（一处实现，别处只调）：一个用户能用的模式 = 全部启用的官方模式
+ 他自己的私有模式。私有模式永远不跨人可见，也永远不进 Config 台的列表。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.db.models.chat_mode import (
    EFFORT_VALUES,
    TOOL_SCOPE_ALL,
    TOOL_SCOPE_RESTRICTED,
    ChatMode,
)
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: 内置模式的 slug。``standard`` 是"不收窄"的那条，``turbo`` 是历史极速模式。
SLUG_STANDARD = "standard"
SLUG_TURBO = "turbo"
BUILTIN_SLUGS = (SLUG_STANDARD, SLUG_TURBO)

#: 未安装的市场项在清单里用这个前缀标记；保存时换成安装后的真实 id。
MARKET_PREFIX = "market:"


@dataclass(frozen=True)
class ChatModeSpec:
    """运行时装配契约——``agent_factory`` 只认这个，不认数据库行。"""

    slug: str
    name: str
    tool_scope: str = TOOL_SCOPE_ALL
    mcp_server_ids: Tuple[str, ...] = ()
    skill_ids: Tuple[str, ...] = ()
    plugin_ids: Tuple[str, ...] = ()
    agent_ids: Tuple[str, ...] = ()
    manual_invoke_enabled: bool = True
    #: 收窄模式下是否保留沙箱代码执行与文件工具；``all`` 作用域不看这个位。
    code_exec_enabled: bool = False
    max_iters: Optional[int] = None
    default_effort: str = "fast"
    effort_locked: bool = False
    prompt_kind: Optional[str] = None
    prompt_text: Optional[str] = None

    @property
    def restricted(self) -> bool:
        """是否收窄工具面。标准模式为 False（跟随 catalog 与用户授权）。"""
        return self.tool_scope == TOOL_SCOPE_RESTRICTED


#: 标准模式的兜底契约：库里那行被删/没播种时也不能让对话起不来。
STANDARD_SPEC = ChatModeSpec(
    slug=SLUG_STANDARD,
    name="标准模式",
    tool_scope=TOOL_SCOPE_ALL,
    code_exec_enabled=True,
    default_effort="fast",
)


def _as_id_list(value: Any) -> List[str]:
    """JSON 列 → 干净的字符串 id 列表（去空白、去重、保序）。"""
    if not isinstance(value, (list, tuple)):
        return []
    seen: Dict[str, None] = {}
    for item in value:
        if isinstance(item, str) and item.strip():
            seen.setdefault(item.strip(), None)
    return list(seen.keys())


class ChatModeService:
    def __init__(self, db: Session):
        self.db = db

    # ── 查询 ────────────────────────────────────────────────────────────

    def list_for_user(self, user_id: Optional[str]) -> List[ChatMode]:
        """一个用户能看见的模式：启用的官方模式 + 他自己的私有模式。"""
        query = self.db.query(ChatMode).filter(ChatMode.deleted_at.is_(None))
        if user_id:
            query = query.filter(
                or_(ChatMode.owner_user_id.is_(None), ChatMode.owner_user_id == user_id)
            )
        else:
            query = query.filter(ChatMode.owner_user_id.is_(None))
        # 官方模式停用即对所有人隐藏；私有模式是本人建的，停用与否由本人掌握，
        # 这里一并按 enabled 过滤，保持"列表里出现的就是能用的"。
        query = query.filter(ChatMode.enabled.is_(True))
        return query.order_by(ChatMode.sort_order.asc(), ChatMode.created_at.asc()).all()

    def list_official(self) -> List[ChatMode]:
        """Config 台的列表：只有官方模式，含停用的（管理员要能重新启用）。"""
        return (
            self.db.query(ChatMode)
            .filter(ChatMode.deleted_at.is_(None), ChatMode.owner_user_id.is_(None))
            .order_by(ChatMode.sort_order.asc(), ChatMode.created_at.asc())
            .all()
        )

    def list_owned(self, user_id: str) -> List[ChatMode]:
        """用户设置页的列表：只有本人建的私有模式。"""
        return (
            self.db.query(ChatMode)
            .filter(ChatMode.deleted_at.is_(None), ChatMode.owner_user_id == user_id)
            .order_by(ChatMode.sort_order.asc(), ChatMode.created_at.asc())
            .all()
        )

    def get_visible(self, mode_id: str, user_id: Optional[str]) -> Optional[ChatMode]:
        row = (
            self.db.query(ChatMode)
            .filter(ChatMode.id == mode_id, ChatMode.deleted_at.is_(None))
            .first()
        )
        if row is None:
            return None
        if row.owner_user_id is not None and row.owner_user_id != user_id:
            return None
        return row

    # ── 运行时解析 ──────────────────────────────────────────────────────

    def resolve(self, slug: Optional[str], user_id: Optional[str]) -> ChatModeSpec:
        """把线上传来的 ``mode_slug`` 解析成装配契约。

        解析不到（空 slug / 已删 / 别人的私有模式）一律回落标准模式——模式是
        产品增强而不是权限边界，解析失败该让对话照常跑，不该 500。
        """
        wanted = (slug or "").strip()
        if not wanted or wanted == SLUG_STANDARD:
            return self._spec_of(self._find_by_slug(SLUG_STANDARD, user_id)) or STANDARD_SPEC
        row = self._find_by_slug(wanted, user_id)
        if row is None:
            logger.info("[chat_mode] slug=%s 不可用，回落标准模式", wanted)
            return self._spec_of(self._find_by_slug(SLUG_STANDARD, user_id)) or STANDARD_SPEC
        return self._spec_of(row) or STANDARD_SPEC

    def _find_by_slug(self, slug: str, user_id: Optional[str]) -> Optional[ChatMode]:
        query = self.db.query(ChatMode).filter(
            ChatMode.slug == slug,
            ChatMode.deleted_at.is_(None),
            ChatMode.enabled.is_(True),
        )
        if user_id:
            query = query.filter(
                or_(ChatMode.owner_user_id.is_(None), ChatMode.owner_user_id == user_id)
            )
        else:
            query = query.filter(ChatMode.owner_user_id.is_(None))
        # 私有模式优先于同名官方模式：用户自己建的那条更贴近他的意图。
        return query.order_by(ChatMode.owner_user_id.isnot(None).desc()).first()

    @staticmethod
    def _spec_of(row: Optional[ChatMode]) -> Optional[ChatModeSpec]:
        if row is None:
            return None
        return ChatModeSpec(
            slug=row.slug,
            name=row.name,
            tool_scope=row.tool_scope or TOOL_SCOPE_ALL,
            mcp_server_ids=tuple(_as_id_list(row.mcp_server_ids)),
            skill_ids=tuple(_as_id_list(row.skill_ids)),
            plugin_ids=tuple(_as_id_list(row.plugin_ids)),
            agent_ids=tuple(_as_id_list(row.agent_ids)),
            manual_invoke_enabled=bool(row.manual_invoke_enabled),
            code_exec_enabled=bool(getattr(row, "code_exec_enabled", False)),
            max_iters=row.max_iters,
            default_effort=row.default_effort or "fast",
            effort_locked=bool(row.effort_locked),
            prompt_kind=row.prompt_kind,
            prompt_text=row.prompt_text,
        )

    # ── 写入 ────────────────────────────────────────────────────────────

    def create(self, payload: Dict[str, Any], owner_user_id: Optional[str]) -> ChatMode:
        slug = self._normalize_slug(payload.get("slug"), payload.get("name"))
        if slug in BUILTIN_SLUGS:
            raise BadRequestError(f"slug「{slug}」是内置模式保留字，请换一个")
        clash = (
            self.db.query(ChatMode)
            .filter(
                ChatMode.slug == slug,
                ChatMode.owner_user_id.is_(None)
                if owner_user_id is None
                else ChatMode.owner_user_id == owner_user_id,
                ChatMode.deleted_at.is_(None),
            )
            .first()
        )
        if clash is not None:
            raise BadRequestError(f"已存在同名模式「{slug}」")

        fields = self._writable_fields(payload, owner_user_id, creating=True)
        self._install_market_refs(fields, owner_user_id)
        row = ChatMode(
            id=f"mode_{uuid.uuid4().hex[:16]}",
            slug=slug,
            owner_user_id=owner_user_id,
            is_builtin=False,
            **fields,
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def update(
        self, mode_id: str, payload: Dict[str, Any], owner_user_id: Optional[str]
    ) -> ChatMode:
        row = self._owned_row(mode_id, owner_user_id)
        fields = self._writable_fields(payload, owner_user_id, creating=False)
        self._install_market_refs(fields, owner_user_id)
        for key, value in fields.items():
            setattr(row, key, value)
        self.db.commit()
        self.db.refresh(row)
        return row

    def delete(self, mode_id: str, owner_user_id: Optional[str]) -> None:
        row = self._owned_row(mode_id, owner_user_id)
        if row.is_builtin:
            raise BadRequestError("内置模式不可删除，可以停用它")
        row.deleted_at = datetime.utcnow()
        self.db.commit()

    def _owned_row(self, mode_id: str, owner_user_id: Optional[str]) -> ChatMode:
        """取一行并校验归属：管理员只碰官方模式，用户只碰自己的。"""
        row = (
            self.db.query(ChatMode)
            .filter(ChatMode.id == mode_id, ChatMode.deleted_at.is_(None))
            .first()
        )
        if row is None:
            raise ResourceNotFoundError("chat_mode", mode_id)
        if row.owner_user_id != owner_user_id:
            # 归属不符一律按"不存在"处理，不泄漏别人有没有这条。
            raise ResourceNotFoundError("chat_mode", mode_id)
        return row

    def _writable_fields(
        self, payload: Dict[str, Any], owner_user_id: Optional[str], creating: bool
    ) -> Dict[str, Any]:
        """把请求体收敛成可写字段。未出现的键在更新时保持原值。"""
        out: Dict[str, Any] = {}

        def take(key: str, value: Any) -> None:
            if value is not None or creating:
                out[key] = value

        if creating or "name" in payload:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise BadRequestError("模式名称不能为空")
            out["name"] = name[:100]
        if creating or "description" in payload:
            out["description"] = str(payload.get("description") or "").strip()[:500]
        if creating or "enabled" in payload:
            out["enabled"] = bool(payload.get("enabled", True))
        if creating or "sort_order" in payload:
            try:
                out["sort_order"] = int(payload.get("sort_order", 100))
            except (TypeError, ValueError):
                out["sort_order"] = 100

        if creating or "tool_scope" in payload:
            scope = str(payload.get("tool_scope") or TOOL_SCOPE_RESTRICTED)
            out["tool_scope"] = scope if scope in (TOOL_SCOPE_ALL, TOOL_SCOPE_RESTRICTED) else TOOL_SCOPE_RESTRICTED
        for key in ("mcp_server_ids", "skill_ids", "plugin_ids", "agent_ids"):
            if creating or key in payload:
                out[key] = _as_id_list(payload.get(key))
        if creating or "manual_invoke_enabled" in payload:
            out["manual_invoke_enabled"] = bool(payload.get("manual_invoke_enabled", True))
        if creating or "code_exec_enabled" in payload:
            out["code_exec_enabled"] = bool(payload.get("code_exec_enabled", False))
        if creating or "max_iters" in payload:
            raw = payload.get("max_iters")
            try:
                # 下限 2：配成 1 会把"回答那一轮"也掐掉，等于永远拿不到结果。
                out["max_iters"] = max(2, int(raw)) if raw not in (None, "") else None
            except (TypeError, ValueError):
                out["max_iters"] = None

        if creating or "default_effort" in payload:
            effort = str(payload.get("default_effort") or "fast")
            out["default_effort"] = effort if effort in EFFORT_VALUES else "fast"
        if creating or "effort_locked" in payload:
            out["effort_locked"] = bool(payload.get("effort_locked", False))

        # 提示词两种来源二选一：绑一个提示词管理里的 tab（prompt_kind，正文在那边
        # 编辑与版本化），或者就地手写一段正文（prompt_text）。互斥——同一行留两个
        # 来源，运行时不知道该听谁的。装配侧优先级 prompt_text > prompt_kind。
        #
        # 版本池的 tab 现在可以新增（prompt_version_service.create_custom_kind），
        # 所以"绑 tab"这条路对自建模式同样走得通，不再只有内置极速能用。
        _wants_kind = "prompt_kind" in payload
        _wants_text = "prompt_text" in payload
        if creating or _wants_kind or _wants_text:
            kind = str(payload.get("prompt_kind") or "").strip()
            if owner_user_id is not None:
                # 私有模式只许手写正文：分类是管理端的东西，绑上等于间接读管理员的
                # 提示词内容，越过了"用户看不到管理端提示词"的边界。
                kind = ""
            text = str(payload.get("prompt_text") or "").strip()
            if kind:
                out["prompt_kind"] = kind
                out["prompt_text"] = None
            elif text:
                out["prompt_text"] = text
                out["prompt_kind"] = None
            elif _wants_kind or _wants_text or creating:
                # 两个都清空 = 显式取消专属提示词，退回默认装配
                out["prompt_kind"] = None
                out["prompt_text"] = None
        take("updated_at", datetime.utcnow())
        return out

    @staticmethod
    def _normalize_slug(raw: Any, fallback_name: Any) -> str:
        """slug 归一：小写、只留 [a-z0-9_-]。空则由名称派生，再空则随机。"""
        candidate = str(raw or "").strip().lower()
        if not candidate:
            candidate = str(fallback_name or "").strip().lower()
        cleaned = "".join(ch if (ch.isalnum() and ch.isascii()) or ch in "-_" else "-" for ch in candidate)
        cleaned = cleaned.strip("-_")
        if not cleaned:
            cleaned = f"mode-{uuid.uuid4().hex[:8]}"
        return cleaned[:64]

    # ── 可选能力清单 ────────────────────────────────────────────────────

    def available_capabilities(self, owner_user_id: Optional[str]) -> Dict[str, Any]:
        """模式可勾选的**全量**能力：MCP / 技能 / 插件 / 子智能体，含未安装的市场项。

        和"子智能体绑定"那份清单的关键差别：这里**不按启停过滤**。模式是一次显式的
        窄授权——管理员在模式里勾一个当前对全局关掉的技能，意思就是"只在这个模式里
        给"，跟能力目录的开关是两回事。技能列表因此绕开 ``is_enabled``。

        市场项（未安装的技能 / 子智能体）也一并列出，带 ``source='market'``；
        保存模式时由 :meth:`_install_market_refs` 按需安装成真实条目。
        """
        from core.db.models import AdminSkill
        from core.services.user_agent_service import UserAgentService

        svc = UserAgentService(self.db)
        resources = svc.list_available_resources(owner_user_id)

        # 插件组件的排除集：一个插件 = 「技能 + 工具」的整体，勾插件即勾了它的
        # 组件。它们不能再出现在散装技能/MCP 清单里，否则同一能力会被重复勾到
        # （list_available_resources 对它给的清单已经排除过，这里的**全量增补**
        # 也必须用同一套排除，之前漏了）。
        plugin_component_skill_ids: set = set()
        try:
            from core.db.models import InstalledPlugin
            from sqlalchemy import or_ as _or

            pq = self.db.query(InstalledPlugin)
            if owner_user_id:
                pq = pq.filter(
                    _or(
                        InstalledPlugin.owner_user_id == owner_user_id,
                        InstalledPlugin.owner_user_id.is_(None),
                    )
                )
            else:
                pq = pq.filter(InstalledPlugin.owner_user_id.is_(None))
            for prow in pq.all():
                cids = prow.component_ids or {}
                plugin_component_skill_ids.update(str(x) for x in (cids.get("skills") or []))
            from core.services.plugin_service import builtin_plugin_component_ids

            _builtin_skill_ids, _ = builtin_plugin_component_ids()
            plugin_component_skill_ids.update(str(x) for x in _builtin_skill_ids)
        except Exception:  # noqa: BLE001
            logger.debug("[chat_mode] 插件组件排除集读取失败", exc_info=True)

        # ── 技能：绕开 is_enabled，拿全量 ──
        installed_skill_ids = {
            str(item.get("id")) for item in (resources.get("skills") or []) if isinstance(item, dict)
        }
        skills = list(resources.get("skills") or [])
        try:
            q = self.db.query(AdminSkill)
            if owner_user_id:
                q = q.filter(
                    or_(
                        AdminSkill.owner_user_id.is_(None),
                        AdminSkill.owner_user_id == owner_user_id,
                    )
                )
            else:
                q = q.filter(AdminSkill.owner_user_id.is_(None))
            for row in q.all():
                if row.skill_id in installed_skill_ids:
                    continue
                if row.skill_id in plugin_component_skill_ids:
                    # 插件自带的技能只通过勾插件获得，不散装列出
                    continue
                installed_skill_ids.add(row.skill_id)
                skills.append(
                    {
                        "id": row.skill_id,
                        "name": row.display_name,
                        "description": row.description or "",
                        # 目录里关掉的技能仍可勾进模式，但要标出来，免得管理员以为配错了
                        "disabled_globally": not bool(row.is_enabled),
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("[chat_mode] 全量技能清单读取失败，退回可用清单", exc_info=True)

        # ── 子智能体：本人的 + 全局管理员的 ──
        agents: List[Dict[str, Any]] = []
        try:
            rows = list(svc.list_admin())
            if owner_user_id:
                rows += list(svc.list_for_user(owner_user_id))
            seen: set = set()
            for a in rows:
                aid = str(a.get("agent_id") or "")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                agents.append(
                    {"id": aid, "name": a.get("name") or aid, "description": a.get("description") or ""}
                )
        except Exception:  # noqa: BLE001
            logger.debug("[chat_mode] 子智能体清单读取失败", exc_info=True)

        market_skills, market_agents, market_plugins = self._market_options(
            installed_skill_ids, {a["id"] for a in agents}
        )

        # 提示词 tab 清单：只发给管理端（owner=None）。普通用户看不到「提示词管理」，
        # 给他一个分类下拉等于让他绑一个自己既看不到内容、也改不了正文的黑盒——
        # 私有模式的提示词只有手写正文一条路。
        prompt_kinds: List[Dict[str, Any]] = []
        if owner_user_id is None:
            try:
                from core.services import prompt_version_service as _pvs

                prompt_kinds = _pvs.all_kinds(self.db)
            except Exception:  # noqa: BLE001
                logger.debug("[chat_mode] 提示词 kind 清单读取失败", exc_info=True)

        return {
            "prompt_kinds": prompt_kinds,
            "mcp": resources.get("mcp_servers") or [],
            "skills": skills,
            "plugins": resources.get("plugins") or [],
            "agents": agents,
            "market_skills": market_skills,
            "market_agents": market_agents,
            "market_plugins": market_plugins,
        }

    def _market_options(
        self, installed_skill_ids: set, installed_agent_ids: set
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """市场里尚未安装的技能 / 子智能体 / 插件。取不到就返回空——市场是锦上添花，
        它挂了不该让模式配置页打不开。

        插件只列**不需要密钥**的：required_secrets 非空的要走能力中心的交互式安装
        （填密钥），"保存时静默装"会装出个没有凭据的空壳。
        """
        skills: List[Dict[str, Any]] = []
        agents: List[Dict[str, Any]] = []
        plugins: List[Dict[str, Any]] = []
        try:
            from core.services.marketplace_service import list_marketplace_skills

            for item in list_marketplace_skills(self.db) or []:
                slug = str(item.get("slug") or "")
                # 内置技能全局常驻、装不了（install 会直接拒），而且已经在上面的
                # 技能清单里了——列出来只会给出一个点了报错的选项。
                if not slug or item.get("builtin") or slug in installed_skill_ids:
                    continue
                if item.get("market_enabled") is False:
                    continue
                skills.append(
                    {
                        "id": f"{MARKET_PREFIX}{slug}",
                        "name": item.get("display_name") or slug,
                        "description": item.get("summary") or "",
                        "source": "market",
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("[chat_mode] 技能市场清单读取失败", exc_info=True)
        try:
            from core.services.agent_market_service import list_marketplace_agents

            for item in list_marketplace_agents(self.db) or []:
                slug = str(item.get("slug") or "")
                if not slug or item.get("market_enabled") is False:
                    continue
                agents.append(
                    {
                        "id": f"{MARKET_PREFIX}{slug}",
                        "name": item.get("display_name") or item.get("name") or slug,
                        "description": item.get("summary") or item.get("description") or "",
                        "source": "market",
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("[chat_mode] 子智能体市场清单读取失败", exc_info=True)
        try:
            from core.services.plugin_service import list_plugins

            for item in list_plugins(self.db, None) or []:
                slug = str(item.get("slug") or "")
                if not slug or item.get("installed") or item.get("market_enabled") is False:
                    continue
                if item.get("required_secrets"):
                    # 要密钥的插件必须交互式安装，跳过（能力中心装好后出现在已装清单）
                    continue
                plugins.append(
                    {
                        "id": f"{MARKET_PREFIX}{slug}",
                        "name": item.get("name") or slug,
                        "description": item.get("description") or item.get("summary") or "",
                        "source": "market",
                    }
                )
        except Exception:  # noqa: BLE001
            logger.debug("[chat_mode] 插件市场清单读取失败", exc_info=True)
        return skills, agents, plugins

    def _install_market_refs(
        self, out: Dict[str, Any], owner_user_id: Optional[str]
    ) -> None:
        """把保存时引用到的市场项就地装成真实条目，并把 id 换成安装后的。

        安装作用域跟模式归属走：官方模式装成全局，私有模式装到本人名下。单条装失败
        只丢那一条（记日志），不让整次保存失败——用户改的是模式，不该被市场的问题挡住。
        """
        for key, installer in (
            ("skill_ids", "skill"),
            ("agent_ids", "agent"),
            ("plugin_ids", "plugin"),
        ):
            ids = out.get(key)
            if not isinstance(ids, list) or not any(str(i).startswith(MARKET_PREFIX) for i in ids):
                continue
            resolved: List[str] = []
            for raw in ids:
                sid = str(raw)
                if not sid.startswith(MARKET_PREFIX):
                    resolved.append(sid)
                    continue
                slug = sid[len(MARKET_PREFIX):]
                try:
                    if installer == "skill":
                        from core.services.marketplace_service import install_marketplace_skill

                        res = install_marketplace_skill(
                            self.db, slug, owner_user_id=owner_user_id
                        )
                        new_id = str(res.get("skill_id") or res.get("id") or "")
                    elif installer == "plugin":
                        from core.services.plugin_service import install_plugin

                        res = install_plugin(self.db, slug, owner_user_id=owner_user_id)
                        new_id = str(res.get("install_id") or "")
                    else:
                        from core.services.agent_market_service import install_marketplace_agent

                        res = install_marketplace_agent(
                            self.db, slug, owner_user_id=owner_user_id
                        )
                        new_id = str(res.get("agent_id") or res.get("id") or "")
                    if new_id:
                        resolved.append(new_id)
                    else:
                        logger.warning("[chat_mode] 市场项 %s 安装后没拿到 id，跳过", sid)
                except Exception:  # noqa: BLE001
                    logger.warning("[chat_mode] 市场项 %s 安装失败，已跳过", sid, exc_info=True)
            # 去重保序
            out[key] = list(dict.fromkeys(resolved))


    # ── 播种与迁移 ──────────────────────────────────────────────────────

    def ensure_builtins(self) -> None:
        """播种内置的标准 / 极速两行，并把历史 ``turbo.*`` 配置搬过来。

        幂等：已存在就不动（管理员改过的配置不会被回滚）。
        """
        existing = {
            row.slug
            for row in self.db.query(ChatMode)
            .filter(ChatMode.owner_user_id.is_(None), ChatMode.deleted_at.is_(None))
            .all()
        }
        created = False
        if SLUG_STANDARD not in existing:
            self.db.add(
                ChatMode(
                    id="mode_builtin_standard",
                    slug=SLUG_STANDARD,
                    name="标准模式",
                    description="完整工具、技能与插件，思考强度可调，适合大部分任务",
                    owner_user_id=None,
                    is_builtin=True,
                    enabled=True,
                    sort_order=10,
                    tool_scope=TOOL_SCOPE_ALL,
                    mcp_server_ids=[],
                    skill_ids=[],
                    plugin_ids=[],
                    agent_ids=[],
                    manual_invoke_enabled=True,
                    # all 作用域不看这个位，置 True 只为语义自洽：标准模式本就带代码能力。
                    code_exec_enabled=True,
                    max_iters=None,
                    default_effort="fast",
                    effort_locked=False,
                    prompt_kind=None,
                )
            )
            created = True
        if SLUG_TURBO not in existing:
            self.db.add(ChatMode(**self._turbo_seed_fields()))
            created = True
        if created:
            self.db.commit()

    @staticmethod
    def _turbo_seed_fields() -> Dict[str, Any]:
        """极速模式内置行的字段，取自历史 ``turbo.*`` SystemConfig 键。

        这是那批键的**最后一次**读取——播种之后本表才是真源，Config 台
        「系统配置 → 极速模式」那组入口随之下线。
        """
        from core.services.system_config import (
            turbo_manual_invoke_enabled,
            turbo_max_iters,
            turbo_mcp_server_ids,
            turbo_plugin_ids,
            turbo_skill_ids,
        )

        try:
            mcp_ids = sorted(turbo_mcp_server_ids())
            skill_ids = list(turbo_skill_ids())
            plugin_ids = list(turbo_plugin_ids())
            manual = turbo_manual_invoke_enabled()
            iters = turbo_max_iters()
        except Exception:  # noqa: BLE001 — 配置层不可用时用内置检索三件套兜底
            logger.warning("[chat_mode] 读 turbo.* 配置失败，极速模式按内置默认播种", exc_info=True)
            mcp_ids = ["internet_search", "retrieve_dataset_content", "web_fetch"]
            skill_ids, plugin_ids, manual, iters = [], [], True, 4

        return {
            "id": "mode_builtin_turbo",
            "slug": SLUG_TURBO,
            "name": "极速模式",
            "description": "只留检索类工具、不思考，秒级直达结果",
            "owner_user_id": None,
            "is_builtin": True,
            "enabled": True,
            "sort_order": 20,
            "tool_scope": TOOL_SCOPE_RESTRICTED,
            "mcp_server_ids": mcp_ids,
            "skill_ids": skill_ids,
            "plugin_ids": plugin_ids,
            "agent_ids": [],
            "manual_invoke_enabled": manual,
            # 极速的产品契约就是"检索直达、不执行代码"，保持关。
            "code_exec_enabled": False,
            "max_iters": iters,
            # 极速那条链路本就不思考，强度锁死——不是"默认不思考但可以改"。
            "default_effort": "fast",
            "effort_locked": True,
            "prompt_kind": "turbo",
        }
