# Workbench application diagnostics

A failed application page offers a diagnostic ID and Retry. Retry checks the current installation and access. It does not replay business writes. Users can select the ID when clipboard access fails. Account changes abort pending diagnostic requests and clear the old receipt.

The application configuration page contains Application diagnostics (EE). Administrators inspect source, revision, installation digest, execution scope, enabled state, authentication type, and recent failures. Enter an existing user ID to evaluate actual directory permissions. Without a user context, permissionStatus remains requires_user_context.

Declaration preview uses the same protocol and semantic checks as external application saves. It does not write configuration. External authentication configuration keeps its existing management interface. Diagnostic projections only expose independent, shared_identity, exchange, or host_session. They omit credentials, URLs, callbacks, and configuration references.

## HTTP interfaces

All paths are relative to /v1/workbench and use the existing API envelope.

| Interface | Authorization and behavior |
| --- | --- |
| GET /admin/diagnostics | Configuration administrator; optional user_id checks an existing user |
| POST /admin/diagnostics/preview | Configuration administrator; JSON declaration; validates without saving |
| POST /admin/diagnostics/probe/{key} | Configuration administrator; probes only a saved external target |
| GET /me/diagnostics | Current user's recent events |
| GET /me/diagnostics/{id} | Receipt owner only; unknown and other-user IDs both return 404 |
| POST /applications/{key}/diagnostics | Currently discoverable application; only page_failed or bridge_timeout |

Failed launches include diagnosticId. Undiscoverable applications never persist the raw requested application name. Browser failure reports are client observations, not proof of a server-confirmed third-party crash.

## Safe probing

Probes send HEAD without credentials. Targets require HTTPS on port 443, without queries or fragments. Every resolved address must be public. Connections use validated numeric addresses, preserve TLS hostname verification, and check the actual peer. All redirects are rejected. The probe does not read response bodies or use environment proxies.

DNS waits at most two seconds, with at most two outstanding resolver jobs. Each connection times out after two seconds, and at most four addresses are attempted. The HTTP standard library also bounds header line length and header count. HEAD reachability does not verify authentication, embedding, or business operations.

Private targets retain their existing browser-opening behavior. The diagnostic server cannot actively probe them.

## Logs and metrics

The diagnostic allowlist includes appKey, instanceId, installId, revision, scope, traceId, event, and error code. User, installation, and instance identities use digests. Logs omit content, file bodies, URLs, tickets, credentials, and exception text.

Approximately the latest 1000 events are retained. Metrics identify that retention window; they are not cumulative billing counters. Metrics cover directory durations, launch success rate, handshake timeouts, permission denials, quota denials, release failures, and active resource instances.

Active resource instances count real application-owned resource leases, not browser pages. The metric is unavailable before the application-owner schema is present. Resource modules use the trusted record_resource observer; a plugin-provided instance ID never grants access.

Batch acceptance must verify PostgreSQL, real browser failure flows, resource event integration, and applicable clients. Fixtures and network doubles do not prove native networking or full-system acceptance.

## Current-user resource observations

GET /v1/workbench/me/diagnostics/resource-metrics only returns the authenticated user's application resource states and valid leases. It does not accept another user identity. registration represents database records. runtime.complete=false and null worker counts mean that the provider lacks a public non-consuming enumeration API.

Batch tests can use isolated tests.workbench_observed_runner_fixture and tests.workbench_resource_observer.install(app). Both require WB19_METRICS_TESTONLY=1 and isolated directories. The backend adapter adds /workbench-test/me/resource-metrics, uses current-user authentication, and forwards the runner control token. Only a loopback ScriptRunner test service is supported.

The observer reads actual process handle returncodes. It never reads logs, paths, or application output. runtime.appOwnerRunningWorkers counts running handles; appOwnerStartingWorkers counts owner records whose process creation is pending. Use these observations only when complete=true and provider=script_runner. Unopened-application checks must assert both running=0 and starting=0. These test-only routes are never registered by production or desktop entrypoints.

Test paths must be absolute directories inside the current source checkout's .scratch/workbench-* tree. Guards resolve actual containment and reject parent traversal, symlinks, and business storage. Both adapters require the flag and a control token. Start the runner with python -m tests.workbench_observed_runner_fixture --port 5219. This entrypoint binds 127.0.0.1 and rejects nonloopback connections.
