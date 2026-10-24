"""Workbench owns plugin upgrade staging and candidate retention."""


class WorkbenchPackagePolicy:
    def lock_configuration(self, db):
        from features.workbench.coordination import configuration
        configuration(db)

    def prepare(self, db, existing, package, source, approved_revision):
        from features.workbench.plugin_upgrades import prepare
        return prepare(db, existing, package, source, approved_revision)

    def reserve_local(self, existing, revision, files):
        from features.workbench.plugin_candidate_storage import local_reserve
        return local_reserve(existing, revision, files)

    def prepare_local(self, db, current, prepared):
        from features.workbench.local_plugin_upgrades import prepare
        return prepare(db, current, prepared)
