"""Capability-flag resolution: personal explicit → team default → system default.

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

Under CE (no teams), ``team_default_permissions_for_user`` returns ``{}``
and resolution degrades to "personal → system default", i.e. exactly the
same as before this feature was introduced.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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


def normalize_team_permissions(payload: Any) -> Dict[str, Any]:
    """Normalize team default permissions: keep only the 6 valid keys, coerce types, drop the rest.

    The returned dict contains only flags the team **explicitly imposes**
    (PUT full-replacement semantics):

    - The 5 boolean flags: values coerced to ``bool``; missing key = the
      team does not impose that flag.
    - ``allowed_apps``: ``list`` → deduplicated list of strings (empty list
      = restricted to "none"); missing key = the team does not restrict app
      visibility.
    """
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in BOOL_CAPABILITY_DEFAULTS:
        if key in payload and payload[key] is not None:
            out[key] = bool(payload[key])
    raw_apps = payload.get("allowed_apps")
    if isinstance(raw_apps, list):
        out["allowed_apps"] = list(dict.fromkeys(str(x) for x in raw_apps))
    return out


def merge_team_permissions(raw_perms_list: Any) -> Dict[str, Any]:
    """Merge multiple teams' default permissions (union / most permissive across teams).

    The argument is an iterable of the raw ``default_permissions`` values of
    each team. Only flags explicitly imposed by at least one team are
    produced: boolean flags are "True if any team is True";
    ``allowed_apps`` is the union of the teams' whitelists.
    """
    merged: Dict[str, Any] = {}
    merged_apps: Optional[List[str]] = None
    for raw in raw_perms_list:
        perms = normalize_team_permissions(raw)
        for key in BOOL_CAPABILITY_DEFAULTS:
            if key in perms:
                merged[key] = bool(merged.get(key, False)) or bool(perms[key])
        if "allowed_apps" in perms:
            merged_apps = list(dict.fromkeys((merged_apps or []) + perms["allowed_apps"]))
    if merged_apps is not None:
        merged["allowed_apps"] = merged_apps
    return merged


def team_default_permissions_for_user(db: Session, user_id: str) -> Dict[str, Any]:
    """Aggregate the default permissions of all teams the user belongs to (union / most permissive).

    No teams / CE / query exception → return ``{}`` (resolution degrades to
    "personal → system default").
    """
    try:
        # CE's PostgreSQL baseline intentionally omits the team tables while
        # keeping this shared module importable.  Isolate the optional lookup
        # in a SAVEPOINT: catching an undefined-table error without rolling it
        # back leaves PostgreSQL's outer transaction aborted, breaking the
        # caller's next otherwise-valid query.
        with db.begin_nested():
            from core.db.repository import TeamRepository

            rows = TeamRepository(db).list_for_user(user_id)
    except Exception:  # noqa: BLE001 — CE has no team table / any exception degrades safely
        return {}
    return merge_team_permissions(
        getattr(team, "default_permissions", None) for team, _role in rows
    )


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
    from core.auth.role_permissions import role_permissions_for_user

    caps = resolve_capabilities(
        meta,
        role_permissions_for_user(db, user_id),
        team_default_permissions_for_user(db, user_id),
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
    from core.auth.role_permissions import role_permissions_for_user
    from core.db.models import UserShadow

    shadow = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
    meta = dict(shadow.extra_data) if (shadow and isinstance(shadow.extra_data, dict)) else {}
    role_defaults = role_permissions_for_user(db, user_id)
    team_defaults = team_default_permissions_for_user(db, user_id)
    caps = resolve_capabilities(meta, role_defaults, team_defaults)
    if settings.edition.edition == "ce":
        caps["can_create_public_kb"] = False
    return caps
