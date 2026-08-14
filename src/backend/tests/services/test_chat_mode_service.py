"""对话模式服务的行为测试。

盯三件容易出错、且出错代价高的事：

1. **可见范围**：私有模式不能跨人可见，也不能进管理端列表。
2. **解析回落**：解析不到的 slug（已删 / 停用 / 别人的私有模式）必须回落标准模式，
   而不是抛错——模式是产品增强，不是权限边界，解析失败该让对话照常跑。
3. **归属校验**：管理端只能改官方模式，用户只能改自己的；越权按"不存在"处理，
   不泄漏别人有没有这条。
"""

import pytest
from core.db.engine import Base
from core.db.models.chat_mode import ChatMode
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.services.chat_mode_service import SLUG_STANDARD, ChatModeService
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    # 只建这张表：ChatMode 的 owner_user_id 外键指向 users_shadow，SQLite 默认不强制
    # 外键，所以不必把整个 schema 拉起来。
    ChatMode.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _official(db, slug: str, **kw) -> ChatMode:
    row = ChatMode(
        id=f"m_{slug}", slug=slug, name=slug, owner_user_id=None,
        tool_scope=kw.pop("tool_scope", "restricted"),
        mcp_server_ids=kw.pop("mcp_server_ids", []),
        skill_ids=[], plugin_ids=[], manual_invoke_enabled=True,
        default_effort=kw.pop("default_effort", "fast"),
        effort_locked=kw.pop("effort_locked", False),
        enabled=kw.pop("enabled", True), sort_order=10, is_builtin=kw.pop("is_builtin", False),
    )
    db.add(row)
    db.commit()
    return row


def test_private_mode_is_invisible_to_others(db):
    svc = ChatModeService(db)
    _official(db, SLUG_STANDARD, tool_scope="all")
    svc.create({"name": "只有我", "slug": "mine-only"}, owner_user_id="u1")

    assert "mine-only" in [m.slug for m in svc.list_for_user("u1")]
    assert "mine-only" not in [m.slug for m in svc.list_for_user("u2")]
    # 管理端列表只有官方模式
    assert "mine-only" not in [m.slug for m in svc.list_official()]


def test_resolve_falls_back_to_standard(db):
    svc = ChatModeService(db)
    _official(db, SLUG_STANDARD, tool_scope="all")
    _official(db, "disabled-one", enabled=False)
    svc.create({"name": "别人的", "slug": "someone-else"}, owner_user_id="u2")

    # 不存在 / 已停用 / 别人的私有模式，一律回落标准模式且不抛错
    for slug in ("no-such-mode", "disabled-one", "someone-else", "", None):
        assert svc.resolve(slug, "u1").slug == SLUG_STANDARD


def test_resolve_returns_the_mode_spec(db):
    svc = ChatModeService(db)
    _official(db, SLUG_STANDARD, tool_scope="all")
    _official(
        db, "turbo-like",
        mcp_server_ids=["internet_search", "web_fetch"],
        default_effort="fast", effort_locked=True,
    )
    spec = svc.resolve("turbo-like", "u1")
    assert spec.restricted is True
    assert spec.mcp_server_ids == ("internet_search", "web_fetch")
    assert spec.effort_locked is True
    # 标准模式不收窄
    assert svc.resolve(SLUG_STANDARD, "u1").restricted is False


def test_ownership_is_enforced(db):
    svc = ChatModeService(db)
    mine = svc.create({"name": "我的"}, owner_user_id="u1")
    official = _official(db, "official-one")

    # 别人改不了我的；管理端（owner=None）也改不了私有模式
    with pytest.raises(ResourceNotFoundError):
        svc.update(mine.id, {"name": "篡改"}, owner_user_id="u2")
    with pytest.raises(ResourceNotFoundError):
        svc.update(mine.id, {"name": "篡改"}, owner_user_id=None)
    # 用户也改不了官方模式
    with pytest.raises(ResourceNotFoundError):
        svc.update(official.id, {"name": "篡改"}, owner_user_id="u1")


def test_builtin_cannot_be_deleted(db):
    svc = ChatModeService(db)
    builtin = _official(db, SLUG_STANDARD, tool_scope="all", is_builtin=True)
    with pytest.raises(BadRequestError):
        svc.delete(builtin.id, owner_user_id=None)


def test_builtin_slug_is_reserved(db):
    svc = ChatModeService(db)
    with pytest.raises(BadRequestError):
        svc.create({"name": "冒名", "slug": SLUG_STANDARD}, owner_user_id="u1")


def test_prompt_source_is_either_kind_or_text(db):
    """提示词两种来源二选一，互斥。

    绑 tab（prompt_kind，正文在提示词管理里版本化）或就地手写（prompt_text）。
    两个都空 = 退回默认装配。同一行不能同时留两个来源，否则运行时不知道听谁的。
    """
    svc = ChatModeService(db)

    # 手写正文
    a = svc.create({"name": "手写", "prompt_text": "你是速查助手"}, owner_user_id="u1")
    assert (a.prompt_text, a.prompt_kind) == ("你是速查助手", None)

    # 绑 tab
    b = svc.create({"name": "绑定", "prompt_kind": "turbo"}, owner_user_id=None)
    assert (b.prompt_kind, b.prompt_text) == ("turbo", None)

    # 两个都给时以 kind 为准，另一个清掉——不留双来源
    c = svc.create(
        {"name": "都给", "prompt_kind": "turbo", "prompt_text": "会被清掉"},
        owner_user_id=None,
    )
    assert (c.prompt_kind, c.prompt_text) == ("turbo", None)

    # 改成手写会解绑 tab
    d = svc.update(b.id, {"prompt_kind": "", "prompt_text": "改成手写"}, owner_user_id=None)
    assert (d.prompt_text, d.prompt_kind) == ("改成手写", None)

    # 两个都清空 = 退回默认装配
    e = svc.update(d.id, {"prompt_kind": "", "prompt_text": ""}, owner_user_id=None)
    assert (e.prompt_text, e.prompt_kind) == (None, None)


def test_mode_can_scope_subagents(db):
    """子智能体和工具/技能/插件一样是模式的一部分。"""
    svc = ChatModeService(db)
    row = svc.create(
        {"name": "带智能体", "agent_ids": ["agent_a", "agent_b", "agent_a"]},
        owner_user_id="u1",
    )
    assert row.agent_ids == ["agent_a", "agent_b"]
    assert svc.resolve(row.slug, "u1").agent_ids == ("agent_a", "agent_b")


def test_max_iters_floor(db):
    """迭代上限最小 2：配成 1 会把回答那一轮也掐掉。"""
    svc = ChatModeService(db)
    row = svc.create({"name": "紧的", "max_iters": 1}, owner_user_id="u1")
    assert row.max_iters == 2
    row2 = svc.update(row.id, {"max_iters": None}, owner_user_id="u1")
    assert row2.max_iters is None


def test_prompt_applies_regardless_of_tool_scope(db):
    """不收窄的模式也能配专属提示词。

    收窄与否（tool_scope）和"要不要换提示词"是两件正交的事：给标准模式换个口吻，
    不该被迫连工具面一起收窄。装配侧（agent_factory）据此在两条分支都读 spec 的
    提示词——这条用例锁住 spec 一定把它带出来。
    """
    svc = ChatModeService(db)
    row = svc.create(
        {"name": "换口吻", "tool_scope": "all", "prompt_text": "你说话简短克制"},
        owner_user_id="u1",
    )
    spec = svc.resolve(row.slug, "u1")
    assert spec.restricted is False          # 工具面不收窄
    assert spec.prompt_text == "你说话简短克制"  # 但提示词照样带出来


def test_private_mode_cannot_bind_prompt_kind(db):
    """私有模式只许手写正文。

    分类（prompt_kind）是管理端「提示词管理」的东西，普通用户看不到那边的内容；
    允许绑定等于让用户间接把管理员的提示词装进自己的模式。写入侧直接丢弃。
    """
    svc = ChatModeService(db)
    row = svc.create({"name": "越界尝试", "prompt_kind": "turbo"}, owner_user_id="u1")
    assert row.prompt_kind is None
    assert row.prompt_text is None
    # 更新也一样
    row2 = svc.update(row.id, {"prompt_kind": "turbo"}, owner_user_id="u1")
    assert row2.prompt_kind is None


def test_code_exec_flag_roundtrip(db):
    """收窄模式的代码执行位：默认关（与历史"收窄=无代码"行为一致），显式开则
    进 spec——agent 工厂据此保留沙箱/文件工具。"""
    svc = ChatModeService(db)
    # 默认关
    row = svc.create({"name": "纯检索", "slug": "retrieval-only"}, owner_user_id="u1")
    assert bool(row.code_exec_enabled) is False
    assert svc.resolve("retrieval-only", "u1").code_exec_enabled is False
    # 显式开 + 更新可关回
    row2 = svc.create(
        {"name": "带代码", "slug": "with-code", "code_exec_enabled": True},
        owner_user_id="u1",
    )
    assert bool(row2.code_exec_enabled) is True
    assert svc.resolve("with-code", "u1").code_exec_enabled is True
    row3 = svc.update(row2.id, {"code_exec_enabled": False}, owner_user_id="u1")
    assert bool(row3.code_exec_enabled) is False
