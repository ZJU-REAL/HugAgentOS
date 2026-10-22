"""Marketplace package resolution with versioned, bundled and uploaded precedence."""
import base64
from core.infra.exceptions import ResourceNotFoundError
from core.capabilities.plugins.management import packages as plugin_packages
from core.capabilities.plugins.packaging import sources as plugin_sources
from core.capabilities.plugins.packaging.archive import extract_plugin_zip
from core.capabilities.plugins.packaging.importer import normalize_plugin_dir


class MarketplacePackageSource:
    def normalize(self, db, slug):
        """Fetch and normalize a plugin by slug: filesystem preset bundle first, DB-published package as fallback."""
        from features.marketplace.versions.store import active, decode
        from features.marketplace.versions.sources import normalized_plugin
        if plugin_sources.resolve_plugin_dir(slug) is None and (
            db is None or plugin_packages.market_row(db, slug) is None
        ):
            raise ResourceNotFoundError("plugin", slug)
        snapshot = active(db, "plugin", slug)
        if snapshot:
            return normalized_plugin(decode(snapshot))
        plugin_dir = plugin_sources.resolve_plugin_dir(slug)
        if plugin_dir is not None:
            return normalize_plugin_dir(plugin_dir)
        if db is not None:
            row = plugin_packages.market_row(db, slug)
            if row is not None:
                with extract_plugin_zip(base64.b64decode(row.package_b64)) as plugin_root:
                    return normalize_plugin_dir(plugin_root)
        raise ResourceNotFoundError("plugin", slug)

    def archive(self, db, slug):
        from features.marketplace.versions.store import active, archive, decode
        snapshot = active(db, "plugin", slug)
        return archive(decode(snapshot)) if snapshot else None
