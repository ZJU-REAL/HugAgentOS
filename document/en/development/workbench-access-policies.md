# Workbench directory and layout policies

Use `GET /v1/workbench/catalog` for the current user's discoverable applications, finite availability reasons, default entries and required entries. Responses are private and must not use shared caches.

The existing personal → role → team → system resolver remains authoritative. Missing or null personal allowed_apps inherits defaults; an empty array hides apps in its existing domain; personal "*" overrides role/team restrictions. That domain includes plan, batch, automation and external apps. Basic chat, projects, files and settings retain their own rules. Lab and deployment conditions remain separate. Creation permission does not replace reading permission. Existing prompt_hub permission options and toolbar behavior remain.

Every `POST /v1/workbench/applications/{key}/launch` checks the current identity again. Unknown or undiscoverable keys return 404 without names. Discoverable disabled or unsupported entries return 409 with a finite reason. A directory entry or administrator access never grants execution. Personal API keys retain capability and application visibility constraints; project/agent keys retain existing route scopes.

## Administration (EE)

The App Configuration screen includes a Workbench layout policy editor. Administrators choose defaults, change default order, require necessary entries or hide entries. These policies govern Workbench discovery and opening; existing plan, batch and automation REST APIs retain their original authentication, ownership and capability checks.

Fixed utilities (chat, settings, help, plan and batch commands) cannot be relocated or hidden through this policy. Plugins cannot claim required entries. Current placement supports existing sidebar pages and configured external applications. Personal pinning and ordering use the separate navigation preferences capability.

- `GET /v1/workbench/admin/catalog`: complete configuration choices.
- `GET /v1/workbench/admin/policy`: policy and revision.
- `PUT /v1/workbench/admin/policy`: revision, defaultPinned, forced and hidden; stale revisions return 409.
- `GET /v1/workbench/admin/legacy-config`: protected complete legacy configuration.
- `GET /v1/workbench/admin/audit`: sanitized configuration audit.

The independent ContentBlock key workbench_policy stores policy. Conditional writes and audit commit together. No historical migration changes. Initial revision 0 preserves the original layout. Configured defaults and required entries determine placement; other existing pages remain in More or the classic user menu. Restart preserves configuration. Revision conflicts require explicit reload.

Public document responses project app configuration per user while retaining other public content. Permission selectors use the protected administrator catalog and preserve original options such as prompt_hub. New ordinary links never receive host credentials; legacy links retain their original login parameter only through the reauthorized legacy adapter. The CE basic directory does not query EE role/team tables.

## Verification

The isolated test_workbench_policy_routes.py covers inheritance, revocation, layout, direct launch, unknown keys, API keys, SQLite/PostgreSQL contention and CE minimal tables. `npm run test:workbench-policy-browser` uses real HTTP and Chromium for administration, both sidebars, identity switches, conflicts, restart and invalidated responses. Mock authentication does not establish formal session-login or native-client validation.
