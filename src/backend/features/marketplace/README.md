# Marketplace

Owns skill, agent, plugin and MCP listings, submissions, review, immutable versions, installation and export. Installed schemas load independently of runtime switches.

API routes are registered by the host central route tables, preserving policy keys and order. `agent_versions` and `skill_versions` apply selected version content; catalog and installation modules contain the underlying operations. Callers use the concrete responsibility module; there are no legacy-path forwarding modules.

Package resolution implements the core package-source interface through `package_source.MarketplacePackageSource`, registered by host composition. Plugin management callers use `plugin_market` directly for listing metadata. Knowledge selection and desktop resources remain host-owned interfaces; remaining legacy core domain references are migrated in P5/P6.

State modules own bundle paths and shared constants. MCP curated configuration (`mcp_catalog`) is separate from listing queries (`mcp_listing`), authentication materialization, immutable version publication, installation, and revalidation. Models preserve the original table names and relationships, including listing visibility state.

Validation: affected marketplace lifecycle, version conflict, archive safety and auth route tests; original route/schema snapshots; import and CE guards; CE empty database upgrade and release checks.

The API host is initialized before direct route use; standalone feature-route imports retain the existing eager API initialization limitation.

版本差异统一由 edition.py 选择列表策略、可见性及管理请求模型；CE 只覆盖此入口。
