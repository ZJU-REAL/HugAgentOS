# Workbench directory notifications and revocation

The workbench directory uses private ETags. Clients fetch their authorized projection from `GET /v1/workbench/catalog`, then send `If-None-Match`. An unchanged projection returns 304. Accounts and local/cloud projections have separate validators.

`GET /v1/workbench/catalog/events?revision=<revision>` waits up to 25 seconds and returns only revision and scope. Clients fetch the directory after a notification. Notifications never grant permission. Network reconnection and window focus trigger validation. Conditional polling starts at 60 seconds and backs off to 10 minutes after failures. Notifications and conditional polls do not extend an idle login session.

Successful installation, enablement and workbench configuration changes invalidate the current client's directory immediately. BroadcastChannel helps other browser tabs revalidate. A failed directory request preserves the last valid projection and never changes navigation preferences.

Logout, account changes and server changes revoke old instances immediately. The host cancels requests, disconnects MessagePorts and removes old pages synchronously. Unsaved changes cannot block revocation. Host tickets are deleted and resource leases expire before the existing service closes workers. The janitor retries failed cleanup. Third-party systems manage their own sessions.

A retained-instance host can register instances with `createSessionRegistration(ownerScope)`. Capture ownerScope when creating the instance pool; never read the new identity for an old instance. Launch, data and resource APIs continue validating actual ownership, installation, revision and permissions on every request.

## Read-only notifications and resource reconciliation

Directory notifications validate the session and read the directory. They do not revoke credentials or stop workers. Each snapshot owns a database session created and closed in a worker thread.

The existing resource reconciler pages through owners, including users with approvals, tickets or instance records and no running worker. It persists credential revocation before attempting shutdown. Failed shutdown retains the exact resource record for retry. Every operation still checks current authorization independently of the sweep.

PostgreSQL uses a configuration barrier and owner locks. Runtime transactions share the configuration barrier and exclusively lock their owner. Configuration changes, migrations and global package collection exclusively lock the configuration barrier. Different owners can write runtime state concurrently. SQLite retains its single-writer transaction semantics. Runner preflight occurs outside write locks; identity, authorization and quotas are checked again after acquiring the owner lock.
