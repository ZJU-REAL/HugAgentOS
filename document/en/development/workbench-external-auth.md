# External application authentication

Config administrators (EE) configure authentication separately from public declarations. Select a new external application or legacy entry in Application configuration → External application authentication. Writes require the current revision; conflicts return 409. Reads never return the target secret or its hash.

Modes:

- `independent`: open the reviewed target URL. The destination owns login.
- `shared_identity`: set `identityProvider` and the target's exact `loginUrl`. Both systems independently integrate with the same provider. The target executes its existing OIDC/SAML login. Host cookies and reusable tokens are not forwarded.
- `exchange`: set `audience`, exact `callback`, `initiationUrl` and a random target-server `clientSecret` of at least 32 characters. `ttlSeconds` defaults to 30, bounded to 1–60. Login and callback URLs must share the application's HTTP(S) origin. Callback fragments and credential query parameters are rejected.

All endpoints use `/v1/workbench`, the standard envelope and no-store responses. Reverse proxies must preserve the public host and scheme. Browser writes require JSON and the host origin or an explicit `CORS_ORIGINS` entry. Wildcards do not grant credentialed writes.

| Endpoint | Caller / result |
|---|---|
| GET/PUT /admin/external-auth/{key} | Config administrator / redacted settings and revision |
| POST /applications/{key}/launch | Authenticated user / reviewed target initiation URL |
| POST /external-auth/challenges | Authenticated target server / host continuation URL |
| GET /external-auth/continue | Browser / fixed CSP-protected continuation document |
| POST /external-auth/authorize | Authenticated host browser / target callback URL |
| POST /external-auth/exchange | Authenticated target server / subject, audience and applicationKey |

## Required target adapter

The target must implement the complete browser login transaction. Host state registration alone does not prevent login CSRF.

1. Launch returns initiationUrl with a one-time intent in its fragment, bound to the initiating host user and session. At initiationUrl, clear and deliver that intent only to the target's own backend, then generate random browser state. Persist a bounded, single-use target login transaction. On HTTPS, set a Secure, HttpOnly, SameSite=Lax state/session cookie.
2. The target server POSTs `{applicationKey,audience,callback,state,intent}` to challenges with `Authorization: Bearer <target-server-secret>`. Secrets stay off browser pages and logs.
3. Redirect that same browser to data.continueUrl. Challenge and state travel in a fragment. The host clears it, authenticates its own user and consumes the challenge once.
4. The host requires the original initiating user and, for session-backed launches, the exact original session. Account/session changes return launch_identity_mismatch. Cookie-free/native launches require browser authentication as the same user and never borrow a different browser account. Native automatic login has not been verified. The host rechecks current grants and configuration revision, then returns the exact callback with random ticket and state in its fragment. Tickets contain no identity plaintext; the database stores hashes.
5. The target callback clears its fragment before loading other resources. Its backend compares state against the independent pre-login cookie and consumes the target transaction **before** exchange. Reject an uninitiated browser, a different transaction, replay or expiry. A state received alongside a valid ticket is insufficient.
6. The target server POSTs `{applicationKey,audience,callback,ticket,state}` to exchange with its own Bearer secret. Success establishes a separate target session. Failure requires a new login transaction.

Host intent and challenge lifetimes are each 30 seconds. The target transaction lifetime must be bounded and cover challenge plus ticket lifetimes. Production target workers must share their login transaction store. The controlled test adapter has one isolated target worker.

SQLite and PostgreSQL atomically consume challenges and tickets with DELETE RETURNING. Wrong audience, exact callback, state, expiry or revision denies exchange. Current user permissions and application state are checked again. Session tickets also require the original host session to remain valid. Host logout invalidates unredeemed session tickets, but does not log out established third-party sessions.


External targets must use a hostname outside the host session cookie scope. A different port does not isolate cookies. Launch and authorization reject the host/API hostname, the browser host hostname and any matching configured session Domain (including subdomains), with host_cookie_scope_overlap. This includes initiation, callback and shared-provider URLs. Existing global cookie behavior is unchanged. Tests use different hostnames, an actual host session and a host-only HttpOnly sentinel, and verify the target receives neither.

## Legacy migration and recovery

General sso_token URL concatenation is removed. Unconfigured legacy entries report auth_configuration_required, retaining names and definitions. They do not silently become independent logins. Review each target and explicitly save a supported mode. A target expecting the old reusable token requires its target-specific adapter deployed before exchange is enabled. The tested legacy adapter uses the target-initiated challenge protocol. No live configuration migrates automatically.

Authentication uses existing ContentBlock workbench_external_auth: keys. Only credential hashes persist. Challenges and tickets have separate namespaces and expiry. Configuration updates atomically invalidate outstanding challenges/tickets and write redacted audit metadata. No schema migration is needed. Full database snapshots preserve settings; ordinary content exports omit them. Protect backups. After restoring a snapshot, rotate credentials or resave settings to invalidate restored login transactions. Lost plaintext secrets must be rotated.

Before downgrade, freeze writes and preserve accepted settings and revisions. Do not restore generic-token forwarding. Keep the last verified secure implementation if an older target cannot express this protocol.

## Validation limits

Tests cover public HTTP, SQLite/PostgreSQL concurrency, a separate browser origin, real target-server exchange, cookies, replay, uninitiated/different-browser login CSRF, legacy explicit adaptation, logout, secret redaction and restart. Identities are isolated. Shared-provider tests verify the configured target login entry, not real enterprise IdP authentication. Live targets, production identities and native Windows/macOS/UOS clients require separate validation.
