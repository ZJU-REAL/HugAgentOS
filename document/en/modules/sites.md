# Sites (Build Websites in Chat, Hosted by the Platform)

> Last updated: 2026-07-13

**Sites** lets users describe what they need in a conversation and have the agent generate a complete static website and publish it in one step — hosted directly by the platform, accessible to anyone with the link, and updatable later through further conversation. The full pipeline: multi-file site generated in the sandbox → published via the `publish_site` tool → served publicly through an nginx-proxied backend hosting route.

Sites is a **Community Edition (CE)** feature, located in the **Lab** panel (requires Lab access granted by an admin).

## How to use

1. Ask in chat, e.g. "Build a product intro website and publish it." The agent generates a complete static site in the sandbox (HTML/CSS/JS/images with an `index.html` entry), calls the `publish_site` tool, and delivers the access URL (like `/site/<slug>/`) in the conversation.
2. Open **Lab → Sites** to manage all your sites: open, copy link, edit title / address / visibility, delete.
3. To modify a published site, just continue describing changes in chat — the agent republishes with the same `site_id`; the URL stays the same and the version increments.

## Visibility

| Level | Behavior |
|---|---|
| Public (default) | Anyone with the link can view, no sign-in required |
| Team | Visible to members of the selected team when signed in |
| Private | Visible only to the site owner when signed in |

## Versions & rollback

Each publish creates an immutable version directory and the live URL switches in place. **Site management → Versions** lists history with one-click rollback; publishing after a rollback continues from the highest historical version number.

## Light backend (KV & forms)

Sites are more than static pages — the platform ships two built-in in-site APIs (call with relative-path fetch from site JS, no auth setup needed):

- **KV storage** (counters, game scores, light config): `GET/PUT/DELETE __api/kv/<key>`, value ≤ 4KB, ≤ 200 keys per site;
- **Form collection** (comments, signups, feedback): `POST __api/forms/<form_key>` (JSON, ≤ 8KB each, ≤ 5000 per site). Owners view/clear submissions in **Site management → Form data**, or **export CSV to My Space** in one click.

`__api/` is a reserved prefix (site files cannot use it); write operations are rate-limited.

## View statistics

The platform counts HTML page views per site (asset files excluded), shown on the site card and the Share Records page; published sites also appear in a section at the top of Share Records for unified link management.

## Hosting & security

- Site files are stored versioned in the platform storage backend (`sites/<site_id>/v<version>/`), supporting both local and cloud (S3/OSS) storage modes; new versions take effect immediately, and history is retained (local mode keeps the latest 3 versions).
- Requests to `/site/<slug>/…` are proxied by nginx to a public backend hosting route, which enforces visibility, caching, and security response headers.
- Public site responses carry `Content-Security-Policy: sandbox` (without `allow-same-origin`): site scripts run in an opaque origin and cannot call platform APIs with the visitor's credentials; correspondingly, `localStorage` / cookies are unavailable inside the site.
- All site responses carry `X-Robots-Tag: noindex` and are excluded from search engines.

## Limits

- Site content is static files; dynamic capability is limited to the built-in KV and form APIs — no custom server-side logic.
- Per site: ≤ 300 files, ≤ 30MB total, ≤ 10MB per file; ≤ 50 sites per user.
- On intranet deployments external CDN resources may be unreachable; inline or localize styles/scripts (the agent follows this convention by default).

## Key implementation locations

| Part | Location |
|---|---|
| Publish tool | `src/backend/core/llm/tools/site_tool.py` (`publish_site`) |
| Business service | `src/backend/core/services/site_service.py` |
| Public hosting route | `src/backend/api/routes/sites_serve.py` (`GET /site/{slug}/{path}`) |
| Management API | `src/backend/api/routes/v1/sites.py` (`/v1/sites`) |
| Database table | `sites` (`core/db/models/site.py`) |
| Frontend management panel | `src/frontend/src/components/sites/SitesPanel.tsx` (Lab → Sites) |
| nginx forwarding | `location /site/` in `src/frontend/default.conf.template` |

Environment switch: `SITES_ENABLED=false` disables the publish tool entirely (enabled by default).
