# Workbench errors

[中文](../../zh-CN/development/workbench-errors.md)

The SDK rejects requests with a code. HTTP APIs keep the host response envelope and trace ID.
Show the code and safe diagnostic ID. Do not display tokens, file bodies or upstream credentials.
Do not retry writes automatically.

| Code | Meaning | Recovery |
| --- | --- | --- |
| app_not_found | Application is absent or undiscoverable | Refresh the authenticated catalog |
| app_disabled | Installation or definition is disabled | Ask an administrator to enable it |
| permission_denied / capability_denied | Actual authorization rejected | Review policy and approved grants |
| host_incompatible | Version or required capability mismatch | Match SDK/protocol/UI versions |
| origin_unavailable | Bound execution source is unavailable | Restore that exact source |
| revision_conflict | A concurrent writer changed the revision | Read current data; explicitly reapply changes |
| launch_expired | Short-lived launch expired | Request a fresh authorized launch |
| bridge_timeout | Handshake or request timed out | Offer bounded explicit retry |
| session_closed | Host closed or revoked the connection | Stop work and open a fresh instance |
| unknown_method | Method is unsupported | Disable its action |
| invalid_params / invalid_route | Invalid payload or out-of-scope route | Correct fields and stay within the application |
| data_source_denied | Source is undeclared or unapproved | Check data_source:<id> and data.query |
| file_access_denied | Selected file is unavailable or unowned | Select an owned file again |
| cancelled | User or host cancelled work | Preserve safe UI state |
| concurrency_limit / rate_limited | Call budget exceeded | Reduce requests and use bounded retry |
| message_too_large | Message exceeds 256 KiB | Reduce payload; use an authorized file reference |

CLI errors include structure, duplicate_id, duplicate_path, invalid_module, invalid_reference,
unsafe_url, incompatible_version, unsupported_capability, invalid_json, invalid_asset and ui_version.
CLI diagnostics include field paths and exclude input values.
CLI success proves declaration validity. It does not grant host access.
An invalid application key must never trigger a slug-based fallback.
