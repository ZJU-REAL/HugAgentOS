# Workbench UI state and retained instances

A plugin declares its snapshot format with lifecycle.stateSchemaVersion. The host then advertises state.load and state.save. Pages without this declaration can report dirty state, but must not promise draft recovery after reload. Snapshots contain UI settings, filters and routes. Never include passwords, cookies, tokens, private keys or file contents.

The SDK applicationState(connection) helper exposes load, save and setDirty. Load first and save with the returned revision. Saving a snapshot does not clear dirty state. The application calls setDirty(false) after its business save succeeds. A resetRequired result hides the old payload. Explain the reset and obtain the user's choice before submitting reset:true. Never interpret an old format as a new format.

HTTP uses GET/PUT /v1/workbench/me/application-state/{applicationKey}. Query parameters include workspace and packageRevision. Plugins must also supply the exact installId, origin and executionScope. Use the pool's same instanceId for asset and resource lifecycle correlation. The authenticated session supplies the user identity. Requests cannot supply userId.

PUT accepts {revision,schemaVersion,payload,reset?}. Responses contain {revision,schemaVersion,payload,resetRequired}. Revision conflicts and unconfirmed schema resets return 409. Quota failures return 413. Credential fields return 422. Errors never echo the snapshot. State responses prohibit caching.

State is isolated by user, application, installation, origin, execution scope and workspace. Upgrades preserve the installation identity. Format changes require explicit reset. State HTTP accepts the current installation revision only. Retained asset leases do not authorize an older page to overwrite new state. Every state call rechecks authorization after disablement or revocation.

Defaults are 256 KiB per application across its workspaces and 10 MiB per user. WORKBENCH_STATE_APP_BYTES and WORKBENCH_STATE_USER_BYTES can lower these limits. Their maximum values are 1 MiB and 100 MiB. Quotas count JSON UTF-8 bytes. CAS and aggregate quotas share one transaction. The logical state entity uses the existing ContentBlock store with the workbench_application_state namespace. No database migration is required.

Only user activation initializes an instance. Reopening the same binding and workspace reuses it. The default budget retains the current instance and two inactive instances. LRU eviction releases excess instances. Switching or closing a dirty page requires a host discard confirmation. Users can cancel and return to save. Dirty state cannot exceed the budget or block logout, revocation or uninstall cleanup.

Keep WorkspaceInstances mounted across application route changes. Change ownerScope when the account or backend identity changes. Compose its state extension with navigation and resource extensions. Connect bridge ready/failed callbacks to the instance state machine. Release aborts entry.signal synchronously, removes the iframe, and closes ports, resource leases and asset leases for the same entry.instanceId. Retry recreates the current instance only and never replays business writes.
