# Sites (Build Websites in Chat, Hosted by the Platform)

> Last updated: 2026-10-07

**Sites** lets users describe what they need in a conversation and have the agent generate a complete static website and publish it in one step — hosted directly by the platform, accessible to anyone with the link, and updatable later through further conversation. The full pipeline: multi-file site generated in the sandbox → published via the `publish_site` tool → served publicly through an nginx-proxied backend hosting route.

Sites is a **Community Edition (CE)** feature, located in the **Lab** panel (requires Lab access granted by an admin).

## How to use

1. Ask in chat, e.g. "Build a product intro website and publish it." The agent generates a complete static site in the sandbox (HTML/CSS/JS/images with an `index.html` entry), calls the `publish_site` tool, and delivers the access URL (like `/site/<slug>/`) in the conversation.
2. Open **Lab → Sites** to manage all your sites: open, copy link, edit title / address / visibility / access password, delete.
3. To modify a published site, describe the changes in **any** conversation — you don't have to return to the original one. The agent first calls the `list_sites` tool to look up the site ID and source directory, then republishes with that `site_id`; the URL stays the same and the version increments. When the account has several sites and the request is ambiguous, the agent asks which one to change.

## Visibility

| Level | Behavior |
|---|---|
| Public (default) | Anyone with the link can view, no sign-in required |
| Team | Visible to members of the selected team when signed in |
| Private | Visible only to the site owner when signed in |

## Access password

The access password is a gate that is independent of visibility: visibility decides *which signed-in users may see the site*, the password decides *whether someone holding the link must verify first*. Once a public site has a password, visitors first land on a **unified password page** — every password-protected site shares this one page, rendered directly by the backend without any frontend build dependency.

- Set, change or turn it off under **Site management → Settings → Access password**; only the site creator (or the project admin for team-project sites) can do so.
- After a successful unlock the browser holds a credential cookie scoped to that one site, valid for 12 hours; after that the password is required again.
- Changing or removing the password immediately invalidates every credential already issued.
- Anyone who can manage the site skips the gate once signed in.
- The password is stored only as an Argon2id hash — the plaintext is never persisted nor returned by the API, which only reports *whether* a password is set.
- Unlock attempts are rate-limited per IP + site (10 per 5 minutes per backend process; with multiple workers the effective ceiling scales with the worker count).
- Public SQL form endpoints enforce site visibility and password checks. Only tables with explicit visitor insert permission accept submissions; visitors cannot read records.

## Versions & rollback

Each publish creates an immutable version directory and the live URL switches in place. **Site management → Versions** lists history with one-click rollback; publishing after a rollback continues from the highest historical version number.

## Databases and MCP

Static pages do not create databases. For forms or persistent data, the agent uses `manage_application` to create typed SQL tables and connects the page to `/site/<slug>/__api/data/<table>`. Owners inspect schemas and records, import JSON and export CSV in **Site management → Data and MCP**. The application panel lists all owner applications, including data from deleted webpages.

MCP publication is explicit and returns query tools, an endpoint and a separate credential. It does not require a webpage. Database APIs and MCP share one data service. Industry imports require enterprise edition and plugin authorization.

Legacy KV and form collection APIs, tools and UI are removed. See [Application databases and hosted MCP](application-hosting.md) for upgrade archives and restoration. `__api/` remains a reserved prefix.

## View statistics

The platform counts HTML page views per site (asset files excluded), shown on the site card and the Share Records page; published sites also appear in a section at the top of Share Records for unified link management.

## Hosting & security

- Site files are stored versioned in the platform storage backend (`sites/<site_id>/v<version>/`), supporting both local and cloud (S3/OSS) storage modes; new versions take effect immediately, and history is retained (local mode keeps the latest 3 versions).
- Requests to `/site/<slug>/…` are proxied by nginx to a public backend hosting route, which enforces visibility, caching, and security response headers.
- Public site responses carry `Content-Security-Policy: sandbox` (without `allow-same-origin`): site scripts run in an opaque origin and cannot call platform APIs with the visitor's credentials; correspondingly, `localStorage` / cookies are unavailable inside the site.
- All site responses carry `X-Robots-Tag: noindex` and are excluded from search engines.

## Limits

- Site content is static files; dynamic capability uses application SQL APIs and declarative MCP — no custom server-side logic.
- Per site: ≤ 300 files, ≤ 30MB total, ≤ 10MB per file; ≤ 50 sites per user.
- On intranet deployments external CDN resources may be unreachable; inline or localize styles/scripts (the agent follows this convention by default).

## Key implementation locations

| Part | Location |
|---|---|
| Publish and lookup tools | `src/backend/mcp_servers/site_publish_mcp/` (`publish_site` / `list_sites`, shipped by the Sites plugin) |
| Internal endpoints | `src/backend/api/routes/v1/internal_sites.py` (`/v1/internal/sites/publish`, `/v1/internal/sites/list`) |
| Site lookup service | `src/backend/core/services/site_listing.py` (one record shape for local and cloud) |
| Business service | `src/backend/core/services/site_service.py` |
| Public hosting route | `src/backend/api/routes/sites_serve.py` (`GET /site/{slug}/{path}`) |
| Management API | `src/backend/api/routes/v1/sites.py` (`/v1/sites`) |
| Access password | `src/backend/core/services/site_password.py` (hash + credential), `src/backend/api/routes/site_gate.py` (unified password page) |
| Database table | `sites` (`core/db/models/site.py`) |
| Frontend management panel | `src/frontend/src/components/sites/SitesPanel.tsx` (Lab → Sites), `SitePasswordField.tsx` (password management) |
| nginx forwarding | `location /site/` in `src/frontend/default.conf.template` |

Environment switch: `SITES_ENABLED=false` disables the publish tool entirely (enabled by default).

## Site collaboration after project transfer (EE)

After a personal project moves to a team, its sites remain linked to the same project. Site IDs, URLs and historical versions are preserved. No manual source-address update is needed. Application data uses owner authorization and does not automatically inherit team permissions.

Read-only members can inspect project sources and site cards. Editors can change source code and publish new versions of the existing site. Owners and administrators can also change site settings, roll back versions and delete sites. Management permissions are separate from visitor visibility: transfer and republication do not automatically make a private site public or team-visible.

Static team sites publish from saved project sources. For build-based sites, run bash in the team project conversation first; the tool returns the current project working directory. Set `source_dir` to that directory and `src_dir` to a separate build output directory, such as `/workspace/.site-dist`. Publication is rejected when the work copy differs from saved sources or another member changes source during publication; rebuild and retry. Build output does not replace project source code. Explicit team site IDs also require a conversation bound to the corresponding project.

## Concurrency and fault isolation

Site detail checks management permissions without taking an exclusive site-row lock. Publishing, settings updates, rollback and deletion retain write locking. Synchronous database and storage work runs in worker threads so lock waits do not block health checks. A failed view count rolls back that transaction and still returns the loaded page.

PostgreSQL lock waits use `DB_POOL_TIMEOUT` (30 seconds by default). Restart the backend
after changing it so new connections use the setting. This limit is a fallback, not a
replacement for lock-free reads and moving blocking operations off the event loop.

Maintainers can run the contention regression against a dedicated local PostgreSQL test
database. Set `TEST_POSTGRES_URL` in the environment, then run:

```bash
PYTHONPATH=src/backend pytest src/backend/tests/api/test_site_contention_postgres.py -q
```

Tests create and clean up a separate temporary schema and storage directory. They cover
concurrent panel reads, health responsiveness during page and publish lock waits, and successful page delivery after a view-count timeout.
