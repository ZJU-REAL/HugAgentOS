"""Desktop implementation of local skill publication and revision claims."""
from features.desktop.capabilities import skill_paths, skill_publication, skill_views

class DesktopLocalPublication:
    def management_lock_path(self):
        from features.desktop.capabilities.paths import meta_root
        return meta_root() / "management.lock"

    def provision_plugin_assets(self, slug):
        from features.desktop.local.plugins.assets import provision_for
        return provision_for(slug)

    def migrate_local_plugins(self):
        from features.desktop.local.plugins.legacy_migration import migrate
        return migrate()

    def migrate_local_skills(self):
        from features.desktop.local.management_migration import migrate
        return migrate()

    async def ensure_native_servers(self, configs):
        from features.desktop.local.plugins.sidecars import ensure_native_servers
        return await ensure_native_servers(configs)

    def publish_local_plugin(self, definition, *, owner_user_id):
        from features.desktop.capabilities.plugins import publish_local_plugin
        return publish_local_plugin(definition, owner_user_id=owner_user_id)

    def remove_local_plugin(self, slug):
        from features.desktop.capabilities.plugins import remove_local_plugin
        return remove_local_plugin(slug)

    def skill_backend(self, *, priority=150, local=False):
        from features.desktop.skill_backend import CapabilityStoreBackend
        return CapabilityStoreBackend(priority=priority, local=local)

    def merge_skill_infos(self, groups, *, backend_hash):
        from features.desktop.capabilities.skills import merge_skill_infos
        return merge_skill_infos(groups, backend_hash=backend_hash)

    def view_generation(self):
        from features.desktop.capabilities.skills import view_generation
        return view_generation()

    def skill_root(self):
        from features.desktop.capabilities.paths import kind_root
        return kind_root("skill")

    def merge_visible_agents(self, user_id, rows):
        from features.desktop.capabilities.agents import merge_visible
        return merge_visible(user_id, rows)

    def account_agent_definition(self, agent_id):
        from features.desktop.capabilities.agents import account_definition
        return account_definition(agent_id)

    def publish_local_agent(self, definition):
        from features.desktop.capabilities.agents import publish_local_agent
        return publish_local_agent(definition)

    def remove_local_agent(self, agent_id):
        from features.desktop.capabilities.agents import remove_local_agent
        return remove_local_agent(agent_id)

    def rebuild_device_view(self):
        return skill_views.rebuild_device_view()

    def rebuild_user_view(self, user_id):
        return skill_views.rebuild_user_view(user_id)

    def rebuild_views(self, user_id):
        return skill_views.rebuild_views(user_id)

    def remove_local_skill(self, skill_id):
        return skill_publication.remove_local_skill(skill_id)

    def prune_local(self, owners):
        return skill_publication.prune_local(owners)

    def publish_local_skill(self, *args, **kwargs):
        return skill_publication.publish_local_skill(*args, **kwargs)

    def device_view_dir(self):
        return skill_paths.device_view_dir()

    def user_view_dir(self, user_id):
        return skill_paths.user_view_dir(user_id)

    def stamp_installation(self, db, install_id, stamp):
        from features.desktop.models import DeviceCapabilityInstallation
        db.get(DeviceCapabilityInstallation, install_id).created_at = stamp

    def claim_revision(self, db, install_id, expected_revision):
        from features.desktop.models import DeviceCapabilityInstallation
        from sqlalchemy import update
        result = db.execute(update(DeviceCapabilityInstallation).where(
            DeviceCapabilityInstallation.install_id == install_id,
            DeviceCapabilityInstallation.resolved_revision == expected_revision,
            DeviceCapabilityInstallation.state != "removed",
        ).values(generation=DeviceCapabilityInstallation.generation + 1))
        return result.rowcount
