# Browser automation plugin

Install and enable `browser-automation` in the plugin marketplace, then ask the agent to browse, fill forms or test a website. Calling `browser_open` automatically opens the live Chromium session in the right Canvas. Reuse its returned `resource_id` for subsequent calls.

## Use

- Navigate, go back/forward, reload, manage tabs, observe pages, operate locators and capture screenshots.
- Select **Take control** to use the mouse, keyboard, Chinese input and paste. Agent observation and actions pause until **Return to agent**.
- Select **Private login** before taking control for login or verification. **Save login** encrypts the browser state. Explicitly provide the restore ID to `browser_open(checkpoint_id=...)`; passwords and cookies are never tool outputs.
- Uploads total at most 8 MiB. Download through the Canvas file list, or ask the agent to `retain_download` as a conversation artifact. Retained files remain available after the browser closes.
- Closing Canvas stops viewing; **Close browser** ends the session. Reconnect never replays operations. Manual control requires a new lease after disconnection.
- The panel identifies a **Local browser** or **Cloud browser**. Cloud `localhost` refers to the cloud runtime, not the user's computer.

## Runtime and configuration

The host owns authorization, exact installation/version binding, lifecycle, frame transport and the input bridge. Browser policy, tools, skill, scripts and UI remain in the plugin package. Private service endpoints and credentials never reach the iframe.

Docker sandbox images include matching Python Playwright and Chromium. Full desktop packages ship a private browser runtime and do not require user-installed Node.js. Image and offline package builds perform actual form and screenshot checks. Deploy backend, MCP, sandbox images, frontend and affected desktop clients together.

Upgrade the database to `browserassets02` with `alembic upgrade head`. It adds resource, ticket, checkpoint and package tables without rewriting business rows. After backing up and closing browser sessions, revert the application and use `alembic downgrade historydisplay01` if rollback is needed. Downgrade removes the new tables, including saved login states.

| Variable | Default | Purpose |
|---|---|---|
| BROWSER_DNS_RESOLVER_URL | Empty | Optional trusted HTTPS JSON DNS endpoint for Fake-IP networks; resolved IPs still undergo private-network checks |
| `BROWSER_ALLOWED_HOSTS` | Empty | Comma-separated exact host allowlist, including explicit private test hosts |
| `BROWSER_ALLOW_PRIVATE_NETWORK` | `false` | Allow ordinary private networks; link-local/metadata, unspecified and multicast addresses remain blocked |
| `BROWSER_CHROMIUM_SANDBOX` | `true` | Enable Chromium sandbox; container root relies on the outer container boundary |
| `PLAYWRIGHT_CHROMIUM_EXECUTABLE` | Empty | Administrator-selected Chromium path inside the execution environment |
| `PLUGIN_RESOURCE_MAX_PER_USER` | `8` | Active resources per user |
| `PLUGIN_RESOURCE_IDLE_SECONDS` | `1800` | Exit after no viewers or operations |
| `PLUGIN_RESOURCE_TTL_SECONDS` | `86400` | Total resource lifetime |
| `PLUGIN_RESOURCE_RECONCILE_SECONDS` | `30` | Invalid resource reconciliation interval |
| `PLUGIN_RESOURCE_SECRET_KEY` | Empty | Stable encryption key; existing `EMAIL_SECRET_KEY` or `ADMIN_TOKEN` may be used |
| `BACKEND_INTERNAL_TOKEN` | Existing internal callback configuration | Shared backend/built-in MCP invocation signing key; required in cloud deployments. Local runtime may use its random runner key |

Local mode creates a random private data-directory encryption key when none is configured. Keep it to restore saved logins; never embed it in packages or source. Cloud mode refuses browser creation/actions without encryption and invocation signing keys.

Public destinations are allowed by default. The SOCKS transport validates resolved IP addresses and connects to those same checked addresses. Default desktop permission presets additionally deny reads of local state and credential directories while admitting required runtime dependencies. Missing OS confinement fails closed.

Module scripts and styles use short-lived, read-only asset tickets bound to the resource, authenticated session, and installed revision. Logout revokes access. The iframe stays isolated and cannot call business APIs directly. The skill includes workflow and parameter references; all four connector tools expose descriptions. Reopen browsers after updating the installed plugin revision.


### Browser panel (1.0.2)

The tab bar reuses the MIT [chrome-tabs](https://github.com/adamschwartz/chrome-tabs) stylesheet and SVG markup.
The inline plus opens a real tab. Selecting a tab switches pages; its close button closes that tab.
The address bar supports Enter, back, forward and reload. Separate takeover, login, save-login and browser-close buttons are removed.

The remote viewport follows Canvas dimensions. Direct clicks, scrolling and typing automatically acquire connection-bound user control.
Agent observation pauses during human control. Focus leaving the panel or 15 seconds of inactivity returns control.
IME composition and active pointer dragging prevent automatic release.
This panel has no save-login entry and does not save logins automatically. Existing checkpoint IDs can still restore a session.


### Single tab strip and scrolling (1.0.3)

The browser panel shows one web tab strip. Fullscreen, collapse, and switching to other Canvas content share that row.
A generic canvas_header declaration lets the module provide its header without an enclosing browser tab row.

The first wheel gesture acquires control and sends pointer coordinates and scroll deltas to the remote page.
Continuous wheel and pointer movement are coalesced to avoid saturating the command queue.
Queue overload and connection loss have separate reports; reconnection clears stale connection errors.


### 1.0.4 Unified tabs and launcher

All Canvas tabs use the same MIT Chrome Tabs appearance. Plus opens a shared host launcher without creating a blank browser page. Confirming a URL creates a browser tab. Other entries upload and preview files, open the capability centre, prepare enabled plugin shortcuts in the composer, or restore open content. Cancel preserves mounted content and unsaved edits. Shortcuts require the user to send the prepared message. Unavailable functions such as a terminal are not offered.


### 1.0.5 Address navigation and scrolling fixes

The browser address bar trims whitespace and adds HTTPS to addresses without a scheme. On first open, an address hint appears. Opening the first URL from the launcher reuses the initial tab without leaving an extra blank page. Manual navigation returns when the main document commits while the page continues loading, so slow scripts do not block subsequent operations. Agent navigation still waits for DOM readiness. Native scrollbars are visible and support wheel scrolling and dragging. An unchanged viewport size does not interrupt input. Update the plugin and reopen the browser to use the new version.

### 1.0.6 Browser action response optimization

Actions return cached tab metadata instead of waiting for title reads from every page. Load events and bounded background refresh update titles. Busy pages retain their last known titles without blocking operations in other tabs. Dynamic titles appear after background refresh. Agent navigation still waits for DOM readiness. Locator, control ownership and network policies retain their existing semantics. Reopen browser sessions after upgrading the plugin.

### 1.0.7 Mouse wheel and Bing search

The mouse wheel continues to work at the same position after clicking a page. The hidden input no longer intercepts pointer events; keyboard and IME input remain available. The address bar and new-tab input share URL/search classification: domains, IP addresses, localhost and explicit HTTP(S) URLs navigate directly; other text searches Bing. Search is sent only on submission, with no suggestion requests. Unsafe schemes and URLs containing credentials are rejected.

### 1.0.8 Zoom and touch scrolling

New tabs start at 100%. Use Ctrl plus/minus to change zoom and Ctrl zero to reset; Command is also supported on macOS. Toolbar buttons provide zoom out, percentage reset and zoom in. Zoom changes the remote layout viewport and fits the complete frame into the panel without cropping its edges. Content beyond the viewport remains scrollable. Tabs keep separate zoom levels within the current panel; available steps respect viewport size limits.

A touch tap clicks and a one-finger drag scrolls without starting mouse text selection. Multitouch and cancelled gestures do not click. IME, mouse selection and wheel input remain available. Reopen browser sessions after upgrading.

### 1.0.9 Tab selection fixes

Pages opened by links or popups automatically become the selected Canvas browser page and live frame. Closing the selected tab activates its right neighbor, or its left neighbor when no right neighbor exists. Closing a background tab preserves selection. Tab changes update the address bar and focus address entry for blank pages; metadata updates in the same tab preserve unfinished input. Crowded narrow tab strips scroll horizontally and bring the selected tab into view. Plus still opens the host launcher first; cancelling creates no page. Reopen browser sessions after updating the plugin.


### Desktop local installation

In hybrid mode, a cloud-installed browser plugin uses a cloud browser. To use a local browser, import the `browser-automation` package through the plugin manager's local installation entry and load the complete local installation identifier. This distinguishes it from a cloud installation with the same name. User-owned local plugins participate in conversation assembly; legacy shared local bootstrap plugins do not override account capabilities.

When a user-owned local plugin is loaded, the desktop service starts its declared bundled MCPs on loopback on demand with the existing branded port namespace, watchdog and process cleanup. Business connectors continue through the cloud gateway. When invoking a built-in MCP, the gateway issues a fresh invocation proof for the authenticated account and current conversation instead of forwarding a device-issued proof to cloud internal services.
