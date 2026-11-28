"""Capability-flag resolution with edition-provided default layers.

The system exposes user capability flags consistently across the Config admin
console's user, team, and role management panels:

| Key                 | Type | System default | Meaning                                        |
|---------------------|------|----------------|------------------------------------------------|
| ``lab_enabled``     | bool | ``True``       | Lab module visible                             |
| ``sites_enabled``   | bool | ``True``       | Sites module visible, manage and publish sites |
| ``can_use_api_key`` | bool | ``False``      | Create/use API keys                            |
| ``can_add_skill``   | bool | ``False``      | Upload / write own private skills, submit them to the skill marketplace |
| ``can_install_market_skill`` | bool | ``False`` | Install skills from the skill marketplace |
| ``can_add_mcp``     | bool | ``False``      | Add own private MCPs, submit them to the MCP marketplace |
| ``can_install_market_mcp`` | bool | ``False`` | Install MCPs from the MCP marketplace |
| ``can_import_plugin``| bool| ``False``      | Import plugin packages (zip / Claude Code / Codex) |
| ``can_install_market_plugin`` | bool | ``False`` | Install plugins from the plugin marketplace |
| ``can_add_agent``   | bool | ``False``      | Build or import own sub-agents, submit them to the sub-agent marketplace |
| ``can_install_market_agent`` | bool | ``False`` | Install sub-agents from the sub-agent marketplace |
| ``can_switch_model`` | bool | ``False``     | User-side chat model switching                 |
| ``can_use_evaluation`` | bool | ``False``   | Task evaluation card, reminder and history marks |
| ``can_use_personal_agent`` | bool | ``False`` | EE account permission for the shared Personal Agent module |
| ``can_use_evolution`` | bool | ``True``   | Evolution console: long-term memory and evolution |
| ``can_use_intent_router`` | bool | ``False`` | "智能路由" chat mode: per-turn intent recognition picks the mode |
| ``can_switch_chat_mode`` | bool | ``True`` | Pick the chat mode in the composer; without it the user is held to ``default_chat_mode`` |
| ``default_chat_mode`` | str/None | ``None`` | Chat mode slug a user starts in (``None`` = 标准模式) |
| ``my_space_upload_limit_mb`` | int/None | ``None`` | Per-file My Space upload cap (MB) |
| ``personal_kb_upload_limit_mb`` | int/None | ``None`` | Per-file private KB upload cap (MB) |
| ``allowed_apps``    | list/None | ``None``  | App visibility whitelist (``None`` = all enabled apps) |

**Teams act only as defaults** (set centrally in Config "Team Management →
Permission Configuration"):

1. If the member's personal ``users_shadow.metadata`` sets the flag
   **explicitly** (key present) → the personal value wins;
2. Otherwise fall back to "the team default of any team the user belongs
   to" — with multiple teams take the **union / most permissive** (on if any
   team turns it on; ``allowed_apps`` is the union of the teams' whitelists);
3. Otherwise the system default.

All read/authorization points (``api/routes/v1/auth.py`` serialization, the
various ``_require_*`` gates, ``config_users`` display) go through here
uniformly, so bare metadata reads cannot bypass team defaults.

CE supplies no organization-scoped layers, so resolution is simply personal
explicit values followed by system defaults.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Optional

from core.config.settings import settings
from sqlalchemy.orm import Session

# Self-add vs market-install bit for each capability kind. Authoring your own needs the
# first; installing from that kind's marketplace needs the second; managing something you
# already have (remove, re-icon, accept an upgrade) is allowed with either.
OWN_FLAG: Dict[str, str] = {
    "skill": "can_add_skill",
    "mcp": "can_add_mcp",
    "plugin": "can_import_plugin",
    "agent": "can_add_agent",
}
MARKET_INSTALL_FLAG: Dict[str, str] = {
    "skill": "can_install_market_skill",
    "mcp": "can_install_market_mcp",
    "plugin": "can_install_market_plugin",
    "agent": "can_install_market_agent",
}


def can_manage_owned(caps: Dict[str, Any], kind: str) -> bool:
    """Either bit of ``kind`` lets the user manage what they already own."""
    return bool(caps.get(OWN_FLAG[kind]) or caps.get(MARKET_INSTALL_FLAG[kind]))


def require_capability(db: Session, user_id: str, flag: str, message: str) -> None:
    """Raise AccessDeniedError (reason ``<flag>_disabled``) unless the user holds ``flag``."""
    from core.infra.exceptions import AccessDeniedError

    if not resolve_user_capabilities(db, str(user_id)).get(flag):
        raise AccessDeniedError(message=message, reason=f"{flag}_disabled")


# Boolean capability flags → system defaults. Feature permissions and admin-console
# access permissions share the same layered resolution rules.
BOOL_CAPABILITY_DEFAULTS: Dict[str, bool] = {
    "lab_enabled": True,
    # Sites module (list, manage, publish). Split from lab_enabled; migration sitesperm01 copied existing lab values.
    "sites_enabled": True,
    "can_use_api_key": False,
    # Skills, MCPs, plugins and sub-agents: authoring your own (upload / write / import /
    # submit to the market) is separate from installing what the market offers.
    # Migrations mktperm01 (skill, MCP) and mktperm02 (plugin, agent) copied the old single
    # bit into the install bits so nobody gained or lost access on upgrade.
    "can_add_skill": False,
    "can_install_market_skill": False,
    "can_add_mcp": False,
    "can_install_market_mcp": False,
    "can_import_plugin": False,
    "can_install_market_plugin": False,
    "can_add_agent": False,
    "can_install_market_agent": False,
    "can_create_private_kb": False,
    "can_create_public_kb": False,
    "can_create_channel_bot": False,
    "can_switch_model": False,
    "can_use_voice": False,  # Dictation is opt-in, including for administrators.
    # 会话评价：评价卡片（采纳 / 需修改 / 不满意）、「温馨提醒」浮窗与历史对话待评价标记。
    "can_use_evaluation": False,
    # 进化控制台：长期记忆（读写）、进化配置与「待我批准的能力」。关闭后这些一并停用。
    # On by default so upgrading changes nobody's memory; migration evoperm01 grants it to roles.
    "can_use_evolution": True,
    "can_run_autonomous_loop": True,  # long-running autonomous loop (open in CE, enabled by default, can be disabled per user/team)
    # 自定义对话模式：授予后「设置 → 模式选择」出现，可自建仅本人可见的模式
    "can_manage_chat_modes": False,
    # 智能路由：模式菜单出现「智能路由」，每轮先识别意图再选模式。还要系统配置
    # ``intent_router.enable`` 打开才生效；服务端每轮都重新校验这一位。
    "can_use_intent_router": False,
    # 模式切换：关闭后输入框的模式菜单锁定，服务端每轮把请求的模式换成 ``default_chat_mode``。
    # On by default so upgrading changes nobody's composer; migration modeperm01 grants it to roles.
    "can_switch_chat_mode": True,
    # EE account policy for Personal Agent; CE admits existing users when enabled.
    "can_use_personal_agent": False,
    "can_system_config": False,
    "can_content_manage": False,
    # Security management plugin (security-manager): authorization flag for the agent side
    # to run read-only queries over global audit/invocation logs.
    # The internal_security routes check this; super_admin implicitly enables it.
    "can_security_view": False,
}


# Storage limits in MB: per-file upload caps and total quotas, for My Space and private KBs.
# Stored next to the boolean bits but numeric: a positive integer is a limit, ``0`` is an
# explicit "no limit" (lets a person or a team override a limit set lower in the chain),
# and an absent key means "not configured". The resolved value is ``None`` (no limit) or
# a positive integer.
FILE_LIMIT_KEYS: tuple[str, ...] = ("my_space_upload_limit_mb", "personal_kb_upload_limit_mb")
QUOTA_KEYS: tuple[str, ...] = ("my_space_quota_mb", "personal_kb_quota_mb")
UPLOAD_LIMIT_KEYS: tuple[str, ...] = (*FILE_LIMIT_KEYS, *QUOTA_KEYS)
MAX_UPLOAD_LIMIT_MB = 1024 * 1024  # 1 TB; rejects typos rather than limiting anything real


def normalize_upload_limit(value: Any) -> Optional[int]:
    """Stored form of a cap: an int in ``[0, MAX_UPLOAD_LIMIT_MB]``, or ``None`` if not set."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_UPLOAD_LIMIT_MB:
        return value
    return None


def upload_limits_of(payload: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """The configured caps in a permissions payload (unset / invalid keys omitted)."""
    out: Dict[str, int] = {}
    for key in UPLOAD_LIMIT_KEYS:
        value = normalize_upload_limit((payload or {}).get(key))
        if value is not None:
            out[key] = value
    return out


def merge_upload_limits(values: Any) -> Optional[int]:
    """Most permissive of several caps: an explicit "no cap" (0) wins, else the largest."""
    configured = [v for v in (normalize_upload_limit(x) for x in values) if v is not None]
    if not configured:
        return None
    return 0 if 0 in configured else max(configured)


# Default chat mode: the slug a user starts in, and the only mode they get without
# ``can_switch_chat_mode``. Set per person, role or team like the caps above: the first
# layer that sets it wins (personal → role → team); unset everywhere → ``None`` (标准模式).
# Several roles or teams with different values: the most capable wins, in
# ``DEFAULT_MODE_PRECEDENCE`` order, then the slug's alphabetical order.
DEFAULT_CHAT_MODE_KEY: str = "default_chat_mode"
DEFAULT_MODE_PRECEDENCE: tuple[str, ...] = ("auto", "standard", "turbo")
_MODE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def normalize_default_mode(value: Any) -> Optional[str]:
    """Stored form of a default mode: a slug-shaped string, or ``None`` if not set."""
    if isinstance(value, str) and _MODE_SLUG_RE.match(value.strip()):
        return value.strip()
    return None


def default_mode_of(payload: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """``{DEFAULT_CHAT_MODE_KEY: slug}`` when the payload sets one, else ``{}``."""
    value = normalize_default_mode((payload or {}).get(DEFAULT_CHAT_MODE_KEY))
    return {DEFAULT_CHAT_MODE_KEY: value} if value else {}


def merge_default_modes(values: Any) -> Optional[str]:
    """Pick one default mode out of several layers' values (see ``DEFAULT_MODE_PRECEDENCE``)."""
    configured = sorted({v for v in (normalize_default_mode(x) for x in values) if v})
    if not configured:
        return None
    rank = {slug: i for i, slug in enumerate(DEFAULT_MODE_PRECEDENCE)}
    return min(configured, key=lambda slug: (rank.get(slug, len(rank)), slug))


# "Force-all" sentinel for personal ``allowed_apps``: the person is explicitly set to
# see all apps, **overriding** team/role app-whitelist restrictions (symmetric with the
# boolean flags' "personal force-on"). Used only in personal metadata; team/role layers
# don't use it (their "unrestricted" = no allowed_apps key stored). During resolution →
# final allowed_apps = None (= all).
ALL_APPS: str = "*"

# Page-level admin permission flags (also members of BOOL_CAPABILITY_DEFAULTS, using the
# same three-tier resolution).
# The only difference from feature-module flags: ``super_admin`` implies both are True.
PAGE_ADMIN_FLAGS: tuple[str, ...] = ("can_system_config", "can_content_manage")

# Marker key on a default layer meaning "this layer is authoritative": when present
# (truthy), resolution uses that layer alone — every boolean flag the layer does not
# grant resolves to ``False``, and personal explicit values, lower layers and system
# defaults are all ignored. ``allowed_apps`` comes from the layer too (absent = all).
# Enterprise sets it on the role layer of users configured "by role".
AUTHORITATIVE_LAYER_KEY: str = "__authoritative__"


def strip_layer_markers(layer: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a copy of a default layer without internal marker keys (for API payloads)."""
    if not isinstance(layer, dict):
        return {}
    return {k: v for k, v in layer.items() if k != AUTHORITATIVE_LAYER_KEY}


def is_authoritative_layer(layer: Optional[Dict[str, Any]]) -> bool:
    return isinstance(layer, dict) and bool(layer.get(AUTHORITATIVE_LAYER_KEY))


def page_admin_flags(
    meta: Optional[Dict[str, Any]],
    caps: Dict[str, Any],
) -> Dict[str, bool]:
    """Get the final effective values of the two page-level admin permissions from resolved capability flags ``caps``.

    - ``can_system_config`` → access to the ``/config`` system configuration console
    - ``can_content_manage`` → access to the ``/admin`` content management console

    ``caps`` must be the result of ``resolve_capabilities(meta, team_defaults)``
    (team defaults already merged in); when ``role == "super_admin"`` both are
    True. Shared by login/session serialization, reusing caps the caller has
    already computed.
    """
    is_super = (meta or {}).get("role") == "super_admin"
    return {flag: bool(is_super or caps.get(flag)) for flag in PAGE_ADMIN_FLAGS}


def resolve_capabilities(
    meta: Optional[Dict[str, Any]],
    *default_layers: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Pure function: given personal metadata + several default layers, compute the final effective capability flags.

    ``default_layers`` are ordered **from highest to lowest priority** (below
    personal explicit, above system default), falling through layer by
    layer — typically
    ``resolve_capabilities(meta, role_defaults, team_defaults)``, i.e.
    "personal → role union → team default → system default".

    If any layer carries ``AUTHORITATIVE_LAYER_KEY``, that layer alone decides
    (ungranted flags are ``False``; personal values and lower layers are ignored).

    Backward compatible: the old call
    ``resolve_capabilities(meta, team_defaults)`` keeps its single-layer
    "personal → team → system" semantics unchanged.
    """
    meta = meta if isinstance(meta, dict) else {}
    layers = [d if isinstance(d, dict) else {} for d in default_layers]

    authoritative = next((layer for layer in layers if is_authoritative_layer(layer)), None)
    if authoritative is not None:
        # Authoritative layer: exactly what it grants, nothing inherited or overridden.
        apps = authoritative.get("allowed_apps")
        return {
            **{key: bool(authoritative.get(key, False)) for key in BOOL_CAPABILITY_DEFAULTS},
            **{
                key: normalize_upload_limit(authoritative.get(key)) or None
                for key in UPLOAD_LIMIT_KEYS
            },
            DEFAULT_CHAT_MODE_KEY: normalize_default_mode(authoritative.get(DEFAULT_CHAT_MODE_KEY)),
            "allowed_apps": [str(x) for x in apps] if isinstance(apps, list) else None,
        }

    result: Dict[str, Any] = {}
    for key, sys_default in BOOL_CAPABILITY_DEFAULTS.items():
        personal = meta.get(key)
        if isinstance(personal, bool):
            result[key] = personal  # personal explicit
            continue
        for layer in layers:  # role → team … (high to low)
            if key in layer:
                result[key] = bool(layer[key])
                break
        else:
            result[key] = sys_default  # system default

    for key in UPLOAD_LIMIT_KEYS:
        # First layer that sets a cap wins (personal → role → team); 0 / unset → no cap.
        stored = next((v for v in map(upload_limits_of, (meta, *layers)) if key in v), {})
        result[key] = stored.get(key) or None

    # Same first-layer-wins rule as the caps: personal → role → team; unset → 标准模式.
    result[DEFAULT_CHAT_MODE_KEY] = next(
        (v[DEFAULT_CHAT_MODE_KEY] for v in map(default_mode_of, (meta, *layers)) if v), None
    )

    raw_apps = meta.get("allowed_apps")
    if raw_apps == ALL_APPS:
        result["allowed_apps"] = None  # personal force-all (overrides upper-layer restrictions)
    elif isinstance(raw_apps, list):
        result["allowed_apps"] = [str(x) for x in raw_apps]  # personal explicit whitelist
    else:
        result["allowed_apps"] = None  # system default = all
        for layer in layers:
            if isinstance(layer.get("allowed_apps"), list):
                result["allowed_apps"] = list(
                    layer["allowed_apps"]
                )  # higher-priority layer whitelist
                break
    return result


def user_has_capability(db: Session, user_id: str, flag: str) -> bool:
    """Whether a user has a given capability flag (personal explicit > role union > team default > system default).

    ``super_admin`` implicitly has everything; empty/missing user → False.
    ``UserShadow`` is loaded only once and its meta reused through
    ``resolve_capabilities``, avoiding a second query. The page-level
    authorization gates (``deps._user_has_meta_flag``) and the security
    plugin (``is_security_viewer``) share this single implementation.
    """
    if not user_id:
        return False
    if settings.edition.edition == "ce" and flag == "can_create_public_kb":
        return False
    from core.db.models import UserShadow

    shadow = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
    if not shadow:
        return False
    return meta_has_capability(db, user_id, shadow.extra_data, flag)


def meta_has_capability(db: Session, user_id: str, meta: Any, flag: str) -> bool:
    """``user_has_capability`` for a caller that already loaded the user's metadata."""
    if settings.edition.edition == "ce" and flag == "can_create_public_kb":
        return False
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("role") == "super_admin":
        return True
    from core.auth.edition import default_capability_layers_for_user

    caps = resolve_capabilities(meta, *default_capability_layers_for_user(db, user_id))
    return bool(caps.get(flag))


def resolve_user_capabilities(db: Session, user_id: str) -> Dict[str, Any]:
    """Load the user's metadata + role union + team defaults and return the final effective capability flags.

    Convenience entry point for authorization gates / serialization points
    to call directly:
    ``resolve_user_capabilities(db, uid)["can_add_skill"]``. The resolution
    chain is "personal → role → team → system" (both role and team degrade
    defensively to ``{}``).
    """
    return _resolve_user_meta_and_capabilities(db, user_id)[1]


def _resolve_user_meta_and_capabilities(
    db: Session, user_id: str
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    from core.auth.edition import default_capability_layers_for_user
    from core.db.models import UserShadow

    shadow = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
    meta = dict(shadow.extra_data) if (shadow and isinstance(shadow.extra_data, dict)) else {}
    caps = resolve_capabilities(meta, *default_capability_layers_for_user(db, user_id))
    if settings.edition.edition == "ce":
        caps["can_create_public_kb"] = False
    return meta, caps


def account_capability_payload(db: Session, user_id: str) -> Dict[str, Any]:
    """Effective permission fields for the current account, as serialized to clients.

    Same field set as ``GET /v1/auth/session/check``: ``allowed_apps``, every
    boolean flag, ``can_use_personal_agent`` (module switch + permission) and the
    two page-admin flags (implied by ``super_admin``). Used by ``GET /v1/me`` so
    API-key callers, which have no session cookie, can read their permissions.
    """
    from personal_agent import access as personal_agent_access

    meta, caps = _resolve_user_meta_and_capabilities(db, user_id)
    return {
        "allowed_apps": caps.get("allowed_apps"),
        **{k: bool(caps.get(k)) for k in BOOL_CAPABILITY_DEFAULTS if k not in PAGE_ADMIN_FLAGS},
        **upload_limit_payload(caps),
        "can_use_personal_agent": personal_agent_access.visible(meta, caps),
        **page_admin_flags(meta, caps),
    }


def upload_limit_payload(caps: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Resolved storage limits for client payloads (``None`` = no limit)."""
    return {key: caps.get(key) for key in UPLOAD_LIMIT_KEYS}


def resolve_storage_limits(db: Session, user_id: str) -> Dict[str, Optional[int]]:
    """The account's effective storage limits in MB (``None`` = no limit).

    ``super_admin`` is never limited, matching how it holds every boolean bit.
    """
    if not user_id:
        return {key: None for key in UPLOAD_LIMIT_KEYS}
    meta, caps = _resolve_user_meta_and_capabilities(db, str(user_id))
    unlimited = meta.get("role") == "super_admin"
    return {key: None if unlimited else caps.get(key) for key in UPLOAD_LIMIT_KEYS}


def _too_large(detail: str):
    from fastapi import HTTPException

    return HTTPException(status_code=413, detail=detail)


def enforce_upload_limit(db: Session, user_id: str, key: str, size_bytes: int, label: str) -> None:
    """Raise HTTP 413 when ``size_bytes`` exceeds the user's per-file ``key`` cap."""
    limit_mb = resolve_storage_limits(db, user_id)[key]
    if limit_mb and size_bytes > limit_mb * 1024 * 1024:
        raise _too_large(f"文件过大：{label}单个文件上传上限为 {limit_mb} MB")


def user_file_limit_bytes(db: Session, user_id: str) -> Optional[int]:
    """The user's configured per-file cap in bytes (``my_space_upload_limit_mb``), or ``None``.

    The one per-file limit for a user's files wherever they land: My Space, project sources
    and the sandbox's synchronized space copy. Unset means no limit.
    """
    limit_mb = resolve_storage_limits(db, user_id)["my_space_upload_limit_mb"]
    return limit_mb * 1024 * 1024 if limit_mb else None


def _mb(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.1f}".rstrip("0").rstrip(".")


def enforce_quota(
    db: Session, user_id: str, key: str, used_bytes: Callable[[], int], incoming: int, label: str
) -> None:
    """Raise HTTP 413 when adding ``incoming`` bytes would exceed the user's total ``key`` quota.

    ``used_bytes`` is called only when a quota applies, so unlimited accounts never pay
    for measuring usage.
    """
    if incoming <= 0:
        return
    quota_mb = resolve_storage_limits(db, user_id)[key]
    if not quota_mb:
        return
    used = used_bytes()
    if used + incoming > quota_mb * 1024 * 1024:
        raise _too_large(f"存储空间不足：{label}总容量上限为 {quota_mb} MB，已用 {_mb(used)} MB")
