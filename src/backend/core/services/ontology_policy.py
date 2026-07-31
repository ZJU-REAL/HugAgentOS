"""Instance and user policy for ontology checks during plugin import."""

from __future__ import annotations

from dataclasses import dataclass

from core.db.models import SystemConfig
from core.services.user_service import UserService
from sqlalchemy.orm import Session


FORCE_PLUGIN_IMPORT_BUILD_VALIDATION_KEY = (
    "ontology.force_plugin_import_build_validation"
)


def _as_bool(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PluginImportOntologyValidationPolicy:
    """Effective plugin-import policy after combining instance and user settings."""

    user_enabled: bool
    forced: bool

    @property
    def enabled(self) -> bool:
        return self.forced or self.user_enabled


def plugin_import_build_validation_forced(db: Session) -> bool:
    """Return the independent instance-level plugin import gate."""
    row = (
        db.query(SystemConfig.config_value)
        .filter(SystemConfig.config_key == FORCE_PLUGIN_IMPORT_BUILD_VALIDATION_KEY)
        .first()
    )
    return _as_bool(row[0]) if row is not None else False


def resolve_plugin_import_ontology_validation(
    db: Session,
    user_id: str | None,
) -> PluginImportOntologyValidationPolicy:
    """Resolve whether one plugin import/install must run ontology build checks.

    A private import follows the importing owner's personal ontology switch.
    The instance-level force switch takes precedence. Global/system installs
    have no personal preference and therefore run the check only when forced.
    """
    forced = plugin_import_build_validation_forced(db)
    user_enabled = False
    if user_id:
        user_settings = UserService(db).get_user_settings(user_id)
        user_enabled = bool(user_settings.get("ontology_enabled", False))
    return PluginImportOntologyValidationPolicy(
        user_enabled=user_enabled,
        forced=forced,
    )


def set_plugin_import_build_validation_forced(
    db: Session,
    enabled: bool,
    *,
    updated_by: str = "ontology_governance",
) -> bool:
    """Persist the instance-level force switch and invalidate config caches."""
    row = (
        db.query(SystemConfig)
        .filter(SystemConfig.config_key == FORCE_PLUGIN_IMPORT_BUILD_VALIDATION_KEY)
        .first()
    )
    value = "true" if enabled else "false"
    if row is None:
        row = SystemConfig(
            config_key=FORCE_PLUGIN_IMPORT_BUILD_VALIDATION_KEY,
            config_value=value,
            display_name="强制构建时本体校验",
            description=(
                "插件导入或安装时必须通过激活 Domain Pack 的构建校验；"
                "个人本体开关不能关闭此门禁。"
            ),
            group_key="ontology",
            is_secret=False,
            updated_by=updated_by,
        )
        db.add(row)
    else:
        row.config_value = value
        row.updated_by = updated_by
    db.commit()

    # The generic system-config service may have cached this row for another
    # consumer. Keep both access paths coherent immediately after an admin save.
    from core.services.system_config import SystemConfigService

    SystemConfigService.get_instance().invalidate_cache()
    return enabled
