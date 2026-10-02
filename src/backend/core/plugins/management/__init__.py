"""Public plugin lifecycle API; implementations follow domain ownership."""

from core.plugins.management.admin_config import get_plugin_admin_config, set_plugin_admin_config
from core.plugins.management.details import (
    exclude_market_skill,
    get_market_skill_exclusions,
    get_plugin_detail,
)
from core.plugins.management.installation import (
    ensure_default_plugins_bootstrapped,
    import_plugin,
    import_plugin_from_zip,
    install_plugin,
)
from core.plugins.management.lifecycle import (
    set_installed_plugin_meta,
    set_plugin_component_enabled,
    set_plugin_component_enabled_for_user,
    set_plugin_enabled,
    set_plugin_enabled_for_user,
    uninstall_plugin,
)
from core.plugins.management.market import (
    list_plugins,
    resolve_market_meta,
    set_market_meta,
    set_plugin_market_enabled,
)
from core.plugins.management.packages import delete_market_package, publish_plugin_zip_to_market
from core.plugins.management.queries import get_installed_detail, list_installed
from core.plugins.packaging.definitions import (
    BUILTIN_PLUGIN_MARKET_META,
    DEFAULT_BOOTSTRAP_MARKER_ID,
    DEFAULT_BOOTSTRAP_PLUGIN_SLUGS,
    LOCAL_BOOTSTRAP_MARKER_NAME,
    MAX_ICON_LEN,
    MAX_ICON_URL_LEN,
    MAX_ZIP_BYTES,
    PLUGIN_BUNDLES_DIR,
    PLUGIN_MARKET_META_BLOCK_ID,
    PLUGIN_SOURCE_DIRS,
)
from core.plugins.packaging.sources import builtin_plugin_component_ids

__all__ = [
    "resolve_market_meta",
    "set_market_meta",
    "builtin_plugin_component_ids",
    "list_plugins",
    "set_plugin_market_enabled",
    "list_installed",
    "get_installed_detail",
    "get_plugin_admin_config",
    "set_plugin_admin_config",
    "get_market_skill_exclusions",
    "get_plugin_detail",
    "exclude_market_skill",
    "install_plugin",
    "ensure_default_plugins_bootstrapped",
    "import_plugin",
    "import_plugin_from_zip",
    "publish_plugin_zip_to_market",
    "delete_market_package",
    "uninstall_plugin",
    "set_plugin_enabled",
    "set_plugin_component_enabled",
    "set_plugin_enabled_for_user",
    "set_plugin_component_enabled_for_user",
    "set_installed_plugin_meta",
    "PLUGIN_BUNDLES_DIR",
    "PLUGIN_SOURCE_DIRS",
    "MAX_ZIP_BYTES",
    "DEFAULT_BOOTSTRAP_PLUGIN_SLUGS",
    "DEFAULT_BOOTSTRAP_MARKER_ID",
    "LOCAL_BOOTSTRAP_MARKER_NAME",
    "PLUGIN_MARKET_META_BLOCK_ID",
    "BUILTIN_PLUGIN_MARKET_META",
    "MAX_ICON_LEN",
    "MAX_ICON_URL_LEN",
]
