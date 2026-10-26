"""Marketplace contribution to core capability selection and chat modes."""

class MarketplaceCatalogProvider:
    def plugin_metadata(self, db, slug):
        from features.marketplace.plugin_market import resolve_market_meta
        return resolve_market_meta(db, slug)

    def suspended_mcp_ids(self, db, ids):
        from features.marketplace.models import McpMarketInstallation
        return {
            row[0] for row in db.query(McpMarketInstallation.server_id).filter(
                McpMarketInstallation.server_id.in_(ids),
                McpMarketInstallation.status == "suspended",
            ).all()
        }

    def list_marketplace_skills(self, db):
        from features.marketplace import skill_catalog
        return skill_catalog.list_marketplace_skills(db)

    def list_marketplace_agents(self, db):
        from features.marketplace import agent_catalog
        return agent_catalog.list_marketplace_agents(db)

    def list_plugins(self, db, user_id):
        from features.marketplace import plugin_market
        return plugin_market.list_plugins(db, user_id)

    def install_marketplace_skill(self, db, slug, *, owner_user_id):
        from features.marketplace import skill_installation
        return skill_installation.install_marketplace_skill(db, slug, owner_user_id=owner_user_id)

    def install_marketplace_agent(self, db, slug, *, owner_user_id):
        from features.marketplace import agent_installation
        return agent_installation.install_marketplace_agent(db, slug, owner_user_id=owner_user_id)

    def market_package(self, db, slug):
        from features.marketplace.models import PluginMarketPackage
        if not slug:
            return None
        return db.query(PluginMarketPackage).filter(PluginMarketPackage.slug == slug).first()

    def publish_package(self, db, slug, fields, *, created_by, now):
        from features.marketplace.models import PluginMarketPackage
        from sqlalchemy.orm.attributes import flag_modified
        existing = self.market_package(db, slug)
        if existing is not None:
            for key, value in fields.items():
                setattr(existing, key, value)
            flag_modified(existing, "required_secrets")
            action = "updated"
        else:
            db.add(PluginMarketPackage(slug=slug, created_at=now, created_by=created_by, **fields))
            action = "published"
        db.commit()
        return action

    def delete_package(self, db, slug):
        from core.infra.exceptions import ResourceNotFoundError
        row = self.market_package(db, slug)
        if row is None:
            raise ResourceNotFoundError("plugin_market_package", slug)
        db.delete(row)
        db.commit()

    def excluded_skills(self, db, slug):
        from features.marketplace.models import PluginMarketSkillExclusion
        if db is None:
            return set()
        return {row[0] for row in db.query(PluginMarketSkillExclusion.skill_name).filter(PluginMarketSkillExclusion.slug == slug).all()}

    def exclude_skill(self, db, slug, skill_name, *, created_by):
        from features.marketplace.models import PluginMarketSkillExclusion
        existing = db.query(PluginMarketSkillExclusion).filter(
            PluginMarketSkillExclusion.slug == slug,
            PluginMarketSkillExclusion.skill_name == skill_name,
        ).first()
        if existing is not None:
            return False
        db.add(PluginMarketSkillExclusion(slug=slug, skill_name=skill_name, created_by=created_by))
        db.commit()
        return True

    def mcp_version(self, db, slug):
        from features.marketplace.models import McpMarketItem, McpMarketVersion
        from core.infra.exceptions import ResourceNotFoundError
        item = db.query(McpMarketItem).filter(
            McpMarketItem.slug == slug, McpMarketItem.deleted_at.is_(None),
            McpMarketItem.status == "active",
        ).first()
        if not item:
            raise ResourceNotFoundError("mcp_market_item", slug)
        version = db.query(McpMarketVersion).filter(McpMarketVersion.version_id == item.latest_version_id).first()
        if not version:
            raise ResourceNotFoundError("mcp_market_version", item.latest_version_id)
        return item, version

    def normalize_auth_config(self, config, schema):
        from features.marketplace import mcp_validation
        return mcp_validation.normalize_auth_config(config, schema)

    def installation_secrets(self, version, credentials, existing, *, auth_method):
        from features.marketplace import mcp_credentials
        return mcp_credentials.installation_secrets(version, credentials, existing, auth_method=auth_method)

    async def install_market_item(self, slug, **kwargs):
        from features.marketplace import mcp_installation
        return await mcp_installation.install_market_item(slug, **kwargs)

    def mcp_oauth_admin_client(self, db, slug):
        from features.marketplace import mcp_oauth_clients
        return mcp_oauth_clients.admin_client(db, slug)

    def mcp_oauth_install_policy(self, db, slug, runtime_url):
        from features.marketplace import mcp_oauth_clients, mcp_validation
        item, version = self.mcp_version(db, slug)
        # Same provenance rule as token installs: an unchanged admin-published
        # endpoint may sit on the deployment's own network.
        return {
            "admin_client": mcp_oauth_clients.admin_client(db, slug),
            "allow_private_network": (
                mcp_validation.installs_admin_published_endpoint(item, version, runtime_url)
                or mcp_validation.self_service_private_network_allowed()
            ),
        }
