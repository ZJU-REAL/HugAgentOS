"""Desktop marketplace packages enter the same device-owned installation lifecycle."""

from core.capabilities.plugins.management import packages as plugin_packages
from core.capabilities.plugins.packaging import sources as plugin_sources
import base64
from features.desktop.local.plugins import service as lifecycle
from features.desktop.local.skills.editor import install_archive
from core.infra.exceptions import BadRequestError, ResourceNotFoundError


def install_plugin(db, user_id, slug, secrets):
    from features.marketplace import edition as listing

    listing.ensure_item_visible(db, listing.KIND_PLUGIN, slug, user_id, resource="plugin")
    if secrets:
        raise BadRequestError(message="本机插件凭据请通过连接配置设置，不写入插件包")
    directory = plugin_sources.resolve_plugin_dir(slug)
    if directory is not None:
        return lifecycle.install(user_id, str(directory.resolve()))
    row = plugin_packages.market_row(db, slug)
    if row is None:
        raise ResourceNotFoundError("plugin", slug)
    return install_archive(user_id, base64.b64decode(row.package_b64), "plugin")
