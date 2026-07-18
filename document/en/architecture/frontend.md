# Frontend Architecture

> Last updated: 2026-06-11

The frontend lives in `src/frontend/` and is a React 19 + TypeScript single-page application: built with Vite, styled with Ant Design, state-managed with Zustand, and deliberately router-framework-free — the entry module switches between five independent app shells by URL path. Production builds are served by the Nginx container, and every backend call funnels through one typed client, `api.ts`.

## Multiple App Shells

`src/main.tsx` picks which app to render based on path and query parameters (all wrapped in the antd `ConfigProvider` theme with the zh-CN locale):

| Shell | Trigger | Responsibility |
|---|---|---|
| `App.tsx` | default | The main chat application: sidebar, chat area, capability center, MySpace, projects, lab, etc. |
| `AdminApp.tsx` (Enterprise Edition, EE) | path `/admin` | Content console: skills, prompts, knowledge bases, agents, MCP, billing |
| `ConfigApp.tsx` (Enterprise Edition, EE) | path `/config` | System console: users, teams, invite codes, security audit, license |
| `ApiDocApp.tsx` | path `/api-docs` | Public API documentation page |
| `SharePreviewApp.tsx` | query param `?share` | Read-only chat-share preview page |

The CE derived tree physically removes `AdminApp.tsx` / `ConfigApp.tsx` together with `components/admin/` and `components/config/` (declared in `ce/manifest.yaml`) and ships a slimmed-down `main.tsx` via overlay.

## api.ts — the typed API client

`src/api.ts` (~2,200 lines) is the single gateway to the backend:

- **Base URL**: `getApiUrl()` returns `import.meta.env.VITE_API_BASE_URL || '/api'` — the Vite proxy in development, same-origin `/api` behind Nginx in production;
- **Envelope unwrapping**: the backend uniformly returns `{ code, message, data, trace_id, timestamp }`; `isApiEnvelope` + `unwrapData<T>` extract `data` automatically, so callers receive plain business types;
- **Auth integration**: `onUnauthorized(handler)` registers the 401 callback; `authStore` uses it to centralize the login redirect;
- **Friendly errors**: helpers like `uploadErrorMessage` recognize non-JSON failures such as Nginx 413 (`client_max_body_size` exceeded) and produce readable messages;
- **Domain functions**: hundreds of named functions — session CRUD / streaming and resume / message feedback / knowledge base / artifacts / automation / batch / projects / memories / capability center — each typed against the shared definitions in `types.ts`.

SSE streams bypass the JSON channel of `api.ts`; they are consumed directly from `fetch` streams by `hooks/useStreaming.ts`.

## Component Groups (22 groups under components/)

| Group | Responsibility |
|---|---|
| `admin/` (Enterprise Edition, EE) | Content-console panels: skill / KB / agent managers, page-config editor, icon picker |
| `agent/` | Sub-agent creation page, forms, @-mention popup, panel |
| `apidoc/` | API documentation panel |
| `automation/` | Automation run-timeline panel |
| `batch/` | Batch-execution confirmation modal and progress panel |
| `canvas/` | Data canvas: Univer online-spreadsheet panel |
| `catalog/` | Capability center: skills / MCP pages, catalog panel, skill-marketplace modal, icon picker |
| `chat/` | The chat core: message bubbles, input area, artifact cards, thinking panels, plan cards, skill slash-popup, file-confirm bar |
| `citation/` | Citation badges and citation-aware Markdown / HTML render blocks |
| `common/` | Shared pieces: loading skeleton, auth-expired modal, brand loader, elapsed timer, image preview |
| `config/` (Enterprise Edition, EE) | System-console panels: chat-history review, invite codes, license, sandbox instances, security audit logs |
| `docs/` | App center and docs panel (release notes etc.) |
| `file/` | Attachment cards, file preview pane, MySpace import modal |
| `kb/` | KB creation and reindex modals |
| `lab/` | Lab: automation cards / create / detail / panel |
| `memory/` | Memory facts list |
| `myspace/` | MySpace: document / favorite / image / notification lists and the main panel |
| `projects/` | Project workspaces: cards, detail panel, right rail, memories modal |
| `settings/` | Settings modal, personal API-key panel, teams section |
| `share/` | Share records page |
| `sidebar/` | Sidebar, global search modal, navigation item definitions |
| `tool/` | Tool-call timeline: call rows, thinking rows, output renderer, result panel, inline progress |

## Zustand State (18 stores)

| Store | Responsibility |
|---|---|
| `chatStore` | Session list, current session, message stream (the core state) |
| `authStore` | Login state, current user, 401 redirect and login landing |
| `uiStore` | Global UI: panel toggles, current view, modal state |
| `catalogStore` | Capability catalog (skills / agents / MCP / KB) and toggles |
| `agentStore` | Sub-agent list and selection |
| `settingsStore` | User settings (memory switches, model preferences) |
| `fileStore` | Uploaded attachments and parse state |
| `kbStore` | KB spaces / documents / chunks |
| `mySpaceStore` | MySpace resource tree and favorites |
| `projectStore` | Project list, detail, in-project sessions |
| `batchStore` | Batch-plan state (keyed by plan_id, tracks the pending-confirmation modal) |
| `automationStore` / `automationChatStore` | Automation task list / automation chat run state |
| `canvasStore` | Data-canvas document state |
| `skillDistillStore` | Personal skill-distillation job state |
| `pageConfigStore` | Page configuration (branding, navigation, copy — drives white-labeling) |
| `editionStore` | Consumer of the `/v1/meta/edition` probe: edition and license feature-bit map |
| `modelCapabilitiesStore` | Main-model capability probing (thinking / vision etc.) |

## Hooks

| Hook | Responsibility |
|---|---|
| `useStreaming` | The main SSE consumer: exposes `send` / `abort` / `regenerate` / `editAndResend` / `resumeRunIfAny`; parses `content/thinking/tool_call/tool_result/tool_progress/meta/error` events, maintains text segments and the tool timeline, supports run resumption |
| `useChatActions` | Session-management actions: create / delete / rename / pin & favorite / export / share / summary & classification |
| `useChatInit` | Session initialization and active-run recovery on app start |
| `usePlanMode` | Plan-mode SSE consumer (shared by first execution and resume) |
| `usePageConfig` | Reads page config by dot-path (e.g. `branding.product_name`) |
| `useStallDetector` | Detects a stream gone silent past a threshold (stall hint) |
| `useDelayedFlag` | Anti-flicker delayed boolean (delayed show + minimum hold) |

## Styles and Other Directories

- `styles/`: global CSS split by feature area (`chat.css`, `catalog.css`, `automation.css`, `canvas.css`, `myspace.css`, `projects.css`, `config.css`, `team-folder.css` — 18 files), with `variables.css` for design tokens and `index.ts` importing them all;
- `types.ts` + `types/`: shared types (`ChatMessage`, `CitationItem`, `Catalog`…); team-file domain types in `types/teamFiles.ts`;
- `utils/`: `citations.ts` (citation parsing), `markdown.ts`, `segments.ts` (stream segmentation), `fileParser.ts`, `adminApi.ts`, `pageConfigDefaults.ts` — 22 utility modules;
- `storage.ts`: localStorage persistence and defaults; `appTheme.ts`: antd theme tokens; `preloadReload.ts`: auto-reload on chunk preload failures.

## Internationalization (i18n)

The UI ships in Simplified Chinese / English, powered by a lightweight in-house i18n layer (no third-party dependency):

- `i18n/index.ts`: the `t(text, vars?)` translation function — the original Chinese string is the key, looked up in the English dictionary with fallback to Chinese; `{n}`-style placeholder interpolation;
- `i18n/en/*.ts`: English dictionaries split by domain (chat / catalog / admin / config …), merged and exported by `en/index.ts`;
- The language preference is persisted in localStorage (`jx_lang`); switching reloads the page so module-level constants are re-evaluated in the new language, and antd components follow via `ConfigProvider locale`;
- Switch entry points: Settings → Chat Settings → Display Language in the main app, plus a language button at the top right of the admin consoles; the backend-rendered sign-in page also embeds a bilingual dictionary with its own top-right toggle, sharing the same localStorage key with the frontend;
- Boundary: runtime content from the database (page-config texts, service-config metadata, user data) is not statically translated and renders in its stored language.

## Data Flow: a Message's Journey Through the Frontend

Sending one message ties the store / hook / component layers together:

```
InputArea (components/chat/)
   │ user hits Enter
   ▼
useStreaming.send
   │ 1. chatStore appends the optimistic user message
   │ 2. fetch POST /v1/chats/stream (capabilities, attachments, project context)
   │ parses data: {json} line by line
   │ ├─ content/thinking → appended into segments (utils/segments.ts)
   │ ├─ tool_call/tool_result → tool timeline (components/tool/)
   │ ├─ file_confirm/batch_confirm → confirm bar / modal (hard pause)
   │ └─ meta → citation sources and artifact list written onto the message
   ▼
chatStore update → MessageBubble re-render
   │ markdown rendering (utils/markdown.ts) + citation badges (components/citation/)
   ▼
[DONE] wrap-up: message state finalized; summary/classification and follow-ups fetched
```

Reconnect path: on startup, `useChatInit` calls `getActiveChatRun` to probe for an in-flight Run; if found, it resumes via `followChatRun` (`GET /v1/chats/stream/{run_id}`) from the stored offset, reusing the exact same parsing logic in `useStreaming`.

## Edition (CE/EE) Awareness

The frontend's edition awareness is concentrated in two places:

- `editionStore` fetches `/v1/meta/edition` at startup (an unauthenticated probe) and receives the `{ edition, features }` boolean map; EE-only surfaces (teams, audit, billing entries) show or hide accordingly;
- `pageConfigStore` carries branding and copy configuration (white-labeling); the Community Edition keeps the "Powered by" attribution, while full de-branding is an Enterprise Edition capability (see [Editions](../editions/overview.md)).

## Build and Run

- **Development**: `npm run dev`; the Vite dev server proxies `/api` to `http://localhost:${BACKEND_PORT}` (`vite.config.ts`);
- **Production**: `docker-compose up -d --build frontend` — a multi-stage image runs `npm run build` and hands the output to Nginx, with `VITE_API_BASE_URL` baked into the bundle as a build arg (default `/api`, reverse-proxied by Nginx to the backend container);
- **Fast hot-swap**: build locally with `npm run build`, then `docker cp dist/. hugagent-frontend:/usr/share/nginx/html/` and reload Nginx (see the [Frontend Development Guide](../development/frontend.md)).

## Related Source

| Topic | Path |
|---|---|
| Entry dispatch | `src/frontend/src/main.tsx` |
| Main app shell | `src/frontend/src/App.tsx` |
| API client and envelope unwrapping | `src/frontend/src/api.ts` |
| SSE consumption | `src/frontend/src/hooks/useStreaming.ts` |
| State management | `src/frontend/src/stores/` |
| Component groups | `src/frontend/src/components/` |
| Shared types | `src/frontend/src/types.ts` |
| Build config | `src/frontend/vite.config.ts` |
| Nginx config | `src/frontend/default.conf.template`, `src/frontend/nginx.conf` |
