"""Installed knowledge models and runtime composition."""
NAME = "knowledge"
def setup():
    return True


def register_catalog_options():
    from core.harness.registry.catalog_options import register_catalog_options as register
    from features.knowledge.catalog_options import list_options
    register("kb_spaces", list_options)


def register_retrieval_context():
    from core.harness.registry.retrieval_context import register_retrieval_access_filter
    from features.knowledge.edition import filter_accessible_kb_ids
    register_retrieval_access_filter(filter_accessible_kb_ids)
    from core.harness.registry.retrieval_context import register_retrieval_context as register
    from features.knowledge import edition as external_provider
    register(lambda selected_ids: external_provider.runtime_request_context(selected_ids))
