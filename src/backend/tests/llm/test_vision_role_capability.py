"""视觉桥角色只能指派多模态模型 —— 能力位约束。

`provider_type` 只分得清「对话/向量/重排」，分不清「这个对话模型能不能看图」。角色定义
里的 ``requires`` 补的就是这一层：前端下拉据此收窄，指派接口据此拒绝。

前端过滤是体验，**后端校验才是约束**——直接 PUT 接口能绕过 UI，所以这里重点锁后端。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.db.model_repository import (
    ROLE_DEFINITIONS,
    list_role_assignments,
    provider_has_capability,
    role_required_capability,
)


def _provider(**extra_config):
    return SimpleNamespace(
        provider_id="p1",
        display_name="某模型",
        provider_type="chat",
        extra_config=extra_config or {},
    )


# ── 角色定义 ────────────────────────────────────────────────────────────────


def test_vision_role_requires_supports_vision():
    assert role_required_capability("vision") == "supports_vision"


def test_other_roles_have_no_capability_requirement():
    """别的角色不受影响——加约束不能顺手把主模型也卡住。"""
    for role_key in ROLE_DEFINITIONS:
        if role_key != "vision":
            assert role_required_capability(role_key) is None, role_key


def test_unknown_role_has_no_requirement():
    assert role_required_capability("nope") is None


# ── 能力位判定 ──────────────────────────────────────────────────────────────


def test_capability_check_reads_extra_config():
    assert provider_has_capability(_provider(supports_vision=True), "supports_vision") is True
    assert provider_has_capability(_provider(supports_vision=False), "supports_vision") is False
    # 纯文本模型：压根没这个键
    assert provider_has_capability(_provider(), "supports_vision") is False
    # 有别的能力位但不是要的那个
    assert (
        provider_has_capability(_provider(supports_reasoning_effort=True), "supports_vision")
        is False
    )


def test_capability_check_tolerates_null_extra_config():
    provider = SimpleNamespace(extra_config=None)
    assert provider_has_capability(provider, "supports_vision") is False


def test_empty_capability_means_no_constraint():
    """无要求的角色一律放行，不能因为空字符串把所有指派都拦下。"""
    assert provider_has_capability(_provider(), "") is True


# ── 接口暴露给前端的字段 ────────────────────────────────────────────────────


def test_role_listing_exposes_requires_capability(db_session):
    entries = {e["role_key"]: e for e in list_role_assignments(db_session)}
    assert entries["vision"]["requires_capability"] == "supports_vision"
    assert entries["main_agent"]["requires_capability"] is None
    # 用途字段仍在，前端两条筛选条件是叠加关系
    assert entries["vision"]["required_type"] == "chat"


# ── 指派接口的闸门（绕过 UI 的那条路） ──────────────────────────────────────


def _assign_guard(role_key: str, provider):
    """复刻 assign_role_endpoint 里的能力位判定，返回是否应当拒绝。"""
    required = role_required_capability(role_key)
    return bool(required) and not provider_has_capability(provider, required)


def test_assign_rejects_text_only_provider_for_vision():
    assert _assign_guard("vision", _provider()) is True
    assert _assign_guard("vision", _provider(supports_vision=True)) is False


def test_assign_allows_anything_for_unconstrained_roles():
    assert _assign_guard("main_agent", _provider()) is False
    assert _assign_guard("summarizer", _provider()) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
