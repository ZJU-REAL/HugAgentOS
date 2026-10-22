"""Edition policy selected by the installed distribution."""

def __getattr__(name):
    if name in ('get_hidden_item_ids', 'is_item_visible'):
        import features.marketplace.personal_visibility as provider
        return getattr(provider, name)
    if name in ('KIND_AGENT', 'KIND_MCP', 'KIND_PLUGIN', 'KIND_SKILL', 'annotate_and_filter', 'ensure_item_visible', 'get_disabled_ids', 'set_listing_enabled'):
        import features.marketplace.personal_listing as provider
        return getattr(provider, name)
    raise AttributeError(name)
