"""Edition policy selected by the installed distribution."""

def __getattr__(name):
    if name in ('SiteUpdateScopeFields', 'SitePublishScopeFields', 'resolve_site_scope', 'site_scope_write_fields', 'site_scope_ref', 'serialize_site_scope', 'can_view_site', 'site_management_permission', 'site_listing_predicate'):
        import features.sites.personal_access as provider
        return getattr(provider, name)
    if name in ('SiteScopeMixin', 'site_scope_table_args'):
        import features.sites.personal_scope as provider
        return getattr(provider, name)
    raise AttributeError(name)
