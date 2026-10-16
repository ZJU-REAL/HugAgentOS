# Plugin upgrades and permission approval

An upgrade preserves the workbench entry for the same installation. Reinstalling after uninstall creates a new identity and cannot inherit old entry grants.

The host stages candidates that add application permissions, module grants, keep-alive capability, or change data sources or resource targets. The verified version remains installed. Private plugin owners inspect changes in plugin details and approve the upgrade or keep the previous version. Administrators approve global installations. Approval preserves a disabled plugin's previous choice.

State schema changes require explicit confirmation to start with fresh state. Without confirmation, the old installation remains current. Old state is never interpreted under a new schema. Unsupported host requirements, invalid declarations and missing assets prevent replacement.

Instances bind to exact package revisions. Old instance assets use a 90-second lease, renewed every 30 seconds. New instances use the current revision. Closing an instance immediately denies access. The resource janitor collects old assets after closure and lease expiry. Revocation, uninstall and permission reductions never wait for asset retention.

Authenticated APIs derive the user from authentication context:

- `GET /v1/workbench/plugin-upgrades/{install_id}` returns revisions, permission differences and state schema changes.
- `POST .../{install_id}/decision` accepts `expectedRevision`, `candidateRevision`, `decision=approve|reject`, and optional `resetState`.
- Global installations use `/v1/workbench/admin/plugin-upgrades/...` with existing administrator authorization.
- `POST/PUT /v1/workbench/applications/{key}/revision-leases` binds the current user, full application key, revision and instanceId.
- `POST .../revision-leases/close` safely supports repeated closure.
- Asset requests may reference an established lease through `instanceId`. They cannot create new leases for old revisions.

Decisions must match both the current installation and candidate. Stale decisions return `revision_conflict`; absent pending upgrades return `upgrade_not_pending`; missing schema reset confirmation returns `state_reset_confirmation_required`. Approval credentials are not stored alongside candidate packages.

Candidate limits are 2/100 MiB per installation and 8/200 MiB per owner. Cloud accounting includes archives and the full extraction-cache bound; local accounting includes bundles and inventory. Replacement or rejection reclaims only unreferenced managed candidates, preserving current versions, histories, leases and other installations. Failed cleanup keeps its quota charge and can retry. Excess returns 413 candidate_storage_quota_exceeded without replacing the current or pending version. Pending local candidates do not materialize skill children. Orphaned candidates after uninstall are reclaimed; reinstalling cannot bypass owner limits.

## Lease state and connection recovery

An independent state machine manages revision leases. The host checks instanceId, revision and expiresAt in each receipt. Server expiry and a local monotonic deadline both bound validity. Requests time out after 10 seconds.

Network failures, HTTP 408/429 and transient 5xx responses preserve the loaded page and pause application capabilities while retrying with bounded backoff. Retries never extend the last confirmed lease. Successful renewal resumes the same instance. Deterministic denials such as 401/403/404/409/410 terminate it immediately. Late responses cannot reactivate an expired, closed or identity-revoked instance.

Recovery cancels in-flight bridge calls. Each new call checks lease validity again. The page displays connection recovery status. Resuming a window or changing the system clock cannot restore an expired lease.
