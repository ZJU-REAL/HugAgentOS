"""Team role constants and helpers — single source of truth, to avoid duplicating definitions across modules."""

from __future__ import annotations

from typing import Literal

TeamRole = Literal["owner", "admin", "member"]

TEAM_ROLES: tuple[TeamRole, ...] = ("owner", "admin", "member")

ROLE_RANK: dict[str, int] = {"member": 1, "admin": 2, "owner": 3}

ROLE_LABELS_ZH: dict[str, str] = {"owner": "所有者", "admin": "管理员", "member": "成员"}


def rank(role: str) -> int:
    return ROLE_RANK.get(role, 0)


def role_label(role: str) -> str:
    return ROLE_LABELS_ZH.get(role, role)


def at_least(role: str, minimum: str) -> bool:
    return rank(role) >= rank(minimum)
