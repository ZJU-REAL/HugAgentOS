# Workbench application URLs and plugin navigation

Plugin applications use `/apps/<URL-encoded applicationKey>/*`. Obtain applicationKey from the authorized directory. It identifies the exact installation. Never substitute a display name or plugin slug. Built-in business pages retain their existing URLs.

App Center, both sidebars, and More use the same opening entry. The URL contains the current subpath, query, and fragment. Refresh, deep links, and browser history restore that position. Declare relative `views[].path` values, such as `reports`. The host shows secondary navigation and matches the longest view path for titles and focus.

The standalone SDK exports `workspaceNavigation(connection)`. Call `navigate('/reports/details')` to request navigation within the current application. Use `onChange(listener)` to render the applied host route. Do not echo the same navigation from this callback. Host updates include `{path, requestId}` for acknowledgment and deduplication. Plugins cannot select another application key, an external URL, or a parent directory. The SDK does not expose host stores or credentials.

The page distinguishes directory loading, directory failure, an undiscoverable application, a disabled application, and an unavailable environment. Directory retries retain the URL. Missing applications and denied discovery share one message. Disabled plugins remain absent from the ordinary directory. An authorized exact deep link can show the disabled reason. Launch and asset requests still enforce authorization independently.

A secondary view may reference its application's workspace module. Launch accepts only the main module and modules declared by that application's views. Other installation modules are rejected.

No database schema migration is required. Legacy UI v1/v2, managed application APIs, and existing business URLs retain their contracts.

An empty directory retains the deep link and shows an empty state. Network and server failures offer retry. They do not appear as missing applications or denied discovery.
