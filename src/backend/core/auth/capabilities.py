"""Capability-flag resolution with edition-provided default layers.

The system has 6 user capability flags (consistent with the Config admin
console "User Management" / "Team Management"):

| Key                 | Type | System default | Meaning                                        |
|---------------------|------|----------------|------------------------------------------------|
| ``lab_enabled``     | bool | ``True``       | Lab module visible                             |
| ``can_use_api_key`` | bool | ``False``      | Create/use API keys                            |
| ``can_add_skill``   | bool | ``False``      | Self-add skills in capability center / install from skill marketplace |
| ``can_add_mcp``     | bool | ``False``      | Self-add private MCPs in capability center     |
| ``can_import_plugin``| bool| ``False``      | Install/import plugins                         |
| ``can_add_agent``   | bool | ``False``      | Build own sub-agents / install & publish on sub-agent marketplace |
| ``can_switch_model`` | bool | ``False``     | User-side chat model switching                 |
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

from typing import Any, Dict, Optional

from core.config.settings import settings
from sqlalchemy.orm import Session

# Boolean capability flags → system defaults. The first 5 are "feature module"
# permissions; the last 2 are "admin-console access" permissions (system config /
# content management console), which also support the "personal → team default →
# system default" three-tier resolution.
BOOL_CAPABILITY_DEFAULTS: Dict[str, bool] = {
    "lab_enabled": True,
    "can_use_api_key": False,
    "can_add_skill": False,
    "can_add_mcp": False,
    "can_import_plugin": False,
    "can_add_agent": False,
    "can_create_private_kb": False,
    "can_create_public_kb": False,
    "can_create_channel_bot": False,
    "can_switch_model": False,
    "can_run_autonomous_loop": True,  # long-running autonomous loop (open in CE, enabled by default, can be disabled per user/team)
    "can_system_config": False,
    "can_content_manage": False,
    # Security management plugin (security-manager): authorization flag for the agent side
    # to run read-only queries over global audit/invocation logs.
    # The internal_security routes check this; super_admin implicitly enables it.
    "can_security_view": False,
}

# All capability flag keys (boolean + allowed_apps); the whitelist for team permission configuration and normalization
CAPABILITY_KEYS: tuple[str, ...] = (*BOOL_CAPABILITY_DEFAULTS.keys(), "allowed_apps")

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

    Backward compatible: the old call
    ``resolve_capabilities(meta, team_defaults)`` keeps its single-layer
    "personal → team → system" semantics unchanged.
    """
    meta = meta if isinstance(meta, dict) else {}
    layers = [d if isinstance(d, dict) else {} for d in default_layers]

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
    meta = shadow.extra_data if isinstance(shadow.extra_data, dict) else {}
    if meta.get("role") == "super_admin":
        return True
    from core.auth.edition_capabilities import default_capability_layers_for_user

    caps = resolve_capabilities(
        meta,
        *default_capability_layers_for_user(db, user_id),
    )
    return bool(caps.get(flag))


def resolve_user_capabilities(db: Session, user_id: str) -> Dict[str, Any]:
    """Load the user's metadata + role union + team defaults and return the final effective capability flags.

    Convenience entry point for authorization gates / serialization points
    to call directly:
    ``resolve_user_capabilities(db, uid)["can_add_skill"]``. The resolution
    chain is "personal → role → team → system" (both role and team degrade
    defensively to ``{}``).
    """
    from core.auth.edition_capabilities import default_capability_layers_for_user
    from core.db.models import UserShadow

    shadow = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
    meta = dict(shadow.extra_data) if (shadow and isinstance(shadow.extra_data, dict)) else {}
    caps = resolve_capabilities(meta, *default_capability_layers_for_user(db, user_id))
    if settings.edition.edition == "ce":
        caps["can_create_public_kb"] = False
    return caps
