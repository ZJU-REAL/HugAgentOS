# Workbench application resources

A UI v3 workspace module may declare a packaged resource runtime. An application can open only its declared target module. The module needs resource.attach; commands also need resource.command. Installation approval and current user authorization remain required.

The host attaches createWorkspaceResourceExtension to an opened instance with the complete applicationKey, instanceId, exact installId/packageRevision, origin and executionScope. Never take identity or installation selection from plugin messages. SDK resources(connection) exposes open, renew, command and close. Commands carry a stable operation id. Failed operations with side effects are never automatically replayed.

The host renews an acquired resource every 25 seconds. Unopened applications own no resource timer, iframe or worker. Close, revoke, logout and retention eviction must dispose the extension. Aborting the instance signal clears its timer immediately and then closes its resources. Leases expire after 90 seconds and cannot be revived. Failed cleanup remains closing, consumes quota, and is retried by the existing janitor.

Resources reuse existing /v1/plugin-resources tickets, assets and streams. Lifecycle routes live under /v1/workbench/applications/{key}/instances/{instance}/resources. Create requires Idempotency-Key. Retrying a key returns the same resource; a closed key returns 410 without restarting. Cross-user, installation, revision, instance and execution-scope access is rejected.

The plugin_resources table gains mutually exclusive chat_id or application_key+instance_id ownership. No conversation is fabricated. Upgrades preserve conversation resources. Downgrades refuse to discard application resource records; export and handle these records through an authorized recovery procedure first. Desktop SQLite upgrades at startup; servers use Alembic. Validate copies before changing real data.

Record real provider, PostgreSQL, browser and native-client verification separately. Mock boundary checks do not establish real execution isolation or worker cleanup.
