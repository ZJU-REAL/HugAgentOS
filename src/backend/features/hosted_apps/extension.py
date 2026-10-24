"""Hosted application schema lifecycle composition."""
NAME = "hosted_apps"


def initialize_schema():
    from features.hosted_apps.store import initialize_local_store
    initialize_local_store()


def register_schema():
    from core.harness.registry.schema_lifecycle import register_schema_lifecycle
    register_schema_lifecycle(NAME, initialize_schema)


def setup():
    register_schema()
    return True
