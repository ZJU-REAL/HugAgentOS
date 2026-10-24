"""Expose existing account revision authorization and compare-and-swap semantics."""
from features.desktop.capability import workcopies


class CloudCapabilityRevisions:
    def model(self, db, kind, key):
        return workcopies.get_model(db, kind, key)

    def snapshot_in_session(self, db, user, kind, key):
        return workcopies.snapshot_in_session(db, user, kind, key)

    def snapshot(self, user, kind, key):
        return workcopies.snapshot(user, kind, key)

    def commit(self, user, kind, key, body):
        return workcopies.commit(user, kind, key, body)

    def revision(self, files):
        return workcopies.revision(files)

    def record_id(self, prefix, user, kind, key):
        return workcopies.record_id(prefix, user, kind, key)
