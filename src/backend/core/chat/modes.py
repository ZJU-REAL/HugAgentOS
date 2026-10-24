"""Create, edit and resolve chat modes, including marketplace skill bindings."""
from __future__ import annotations
from core.llm import prompt_kinds
from core.infra.time import utc_now
import uuid
from typing import Any, Dict, List, Optional, Tuple
from core.db.models.chat_mode import TOOL_SCOPE_ALL, ChatMode
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from sqlalchemy import or_
from sqlalchemy.orm import Session
import logging
from core.chat import mode_seed
from core.chat import mode_types
from core.chat import mode_validation

logger = logging.getLogger(__name__)


MARKET_PREFIX = "market:"


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

    def resolve(self, slug: Optional[str], user_id: Optional[str]) -> mode_types.ChatModeSpec:
        """把线上传来的 ``mode_slug`` 解析成装配契约。

        解析不到（空 slug / 已删 / 别人的私有模式）一律回落标准模式——模式是
        产品增强而不是权限边界，解析失败该让对话照常跑，不该 500。
        """
        wanted = (slug or "").strip()
        if not wanted or wanted == mode_types.SLUG_STANDARD:
            return self._spec_of(self._find_by_slug(mode_types.SLUG_STANDARD, user_id)) or mode_types.STANDARD_SPEC
        row = self._find_by_slug(wanted, user_id)
        if row is None:
            logger.info("[chat_mode] slug=%s 不可用，回落标准模式", wanted)
            return self._spec_of(self._find_by_slug(mode_types.SLUG_STANDARD, user_id)) or mode_types.STANDARD_SPEC
        return self._spec_of(row) or mode_types.STANDARD_SPEC

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
    def _spec_of(row: Optional[ChatMode]) -> Optional[mode_types.ChatModeSpec]:
        if row is None:
            return None
        return mode_types.ChatModeSpec(
            slug=row.slug,
            name=row.name,
            tool_scope=row.tool_scope or TOOL_SCOPE_ALL,
            mcp_server_ids=tuple(mode_validation.as_id_list(row.mcp_server_ids)),
            skill_ids=tuple(mode_validation.as_id_list(row.skill_ids)),
            plugin_ids=tuple(mode_validation.as_id_list(row.plugin_ids)),
            agent_ids=tuple(mode_validation.as_id_list(row.agent_ids)),
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
        slug = mode_validation.normalize_slug(payload.get("slug"), payload.get("name"))
        if slug in mode_types.BUILTIN_SLUGS:
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

        fields = mode_validation.writable_fields(payload, owner_user_id, creating=True)
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
        fields = mode_validation.writable_fields(payload, owner_user_id, creating=False)
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
        row.deleted_at = utc_now()
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
        from core.capabilities.agents.edition import UserAgentService

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
            from core.capabilities.plugins.management import builtin_plugin_component_ids

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
        kinds: List[Dict[str, Any]] = []
        if owner_user_id is None:
            try:
                from core.llm import prompt_versions as _pvs

                kinds = prompt_kinds.all_kinds(self.db)
            except Exception:  # noqa: BLE001
                logger.debug("[chat_mode] 提示词 kind 清单读取失败", exc_info=True)

        return {
            "prompt_kinds": kinds,
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
            from core.harness.registry.marketplace_catalog import marketplace_catalog

            for item in marketplace_catalog().list_marketplace_skills(self.db) or []:
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
            from core.harness.registry.marketplace_catalog import marketplace_catalog

            for item in marketplace_catalog().list_marketplace_agents(self.db) or []:
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
            from core.harness.registry.marketplace_catalog import marketplace_catalog

            for item in marketplace_catalog().list_plugins(self.db, None) or []:
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
                        from core.harness.registry.marketplace_catalog import marketplace_catalog

                        res = marketplace_catalog().install_marketplace_skill(
                            self.db, slug, owner_user_id=owner_user_id
                        )
                        new_id = str(res.get("skill_id") or res.get("id") or "")
                    elif installer == "plugin":
                        from core.capabilities.plugins.management import install_plugin

                        res = install_plugin(self.db, slug, owner_user_id=owner_user_id)
                        new_id = str(res.get("install_id") or "")
                    else:
                        from core.harness.registry.marketplace_catalog import marketplace_catalog

                        res = marketplace_catalog().install_marketplace_agent(
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
        if mode_types.SLUG_STANDARD not in existing:
            self.db.add(
                ChatMode(
                    id="mode_builtin_standard",
                    slug=mode_types.SLUG_STANDARD,
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
        if mode_types.SLUG_TURBO not in existing:
            self.db.add(ChatMode(**mode_seed.turbo_seed_fields()))
            created = True
        if created:
            self.db.commit()
