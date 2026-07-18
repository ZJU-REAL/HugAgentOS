# Frontend Development Guide
> Last updated: 2026-06-11

The frontend is React 18 + TypeScript + Ant Design + Zustand (built with Vite), located in `src/frontend/src/`. This page covers the build flow, directory and naming conventions, API call patterns, the steps for adding component groups / stores, and how CE/EE edition gating works. For the big picture see [Frontend Architecture](../architecture/frontend.md).

## Build and hot-swap flow

All services run inside Docker; the frontend is static assets served by an nginx container. After changing code there are two paths:

**Option A — full rebuild** (slower, always correct):

```bash
docker-compose up -d --build frontend
```

**Option B — local build + hot-swap** (faster, needs Node 20+):

```bash
cd src/frontend
npm run build
docker cp dist/. hugagent-frontend:/usr/share/nginx/html/
docker exec hugagent-frontend nginx -s reload
```

Lint:

```bash
cd src/frontend && npm run lint
```

## Directory and naming conventions

```
src/frontend/src/
├── App.tsx              # main application shell
├── AdminApp.tsx         # /admin content console (EE)
├── ConfigApp.tsx        # /config system console (EE)
├── main.tsx             # entry routing (the CE tree uses an overlay version without admin/config)
├── api.ts               # typed API client (envelope unwrapping)
├── types.ts             # shared TypeScript types
├── storage.ts           # localStorage persistence
├── components/          # 22 feature component groups (one directory each + index.ts barrel)
│   ├── chat/ admin/ agent/ automation/ batch/ canvas/ catalog/
│   ├── citation/ common/ config/ docs/ file/ kb/
│   └── lab/ memory/ myspace/ projects/ settings/ share/ sidebar/ tool/ apidoc/
├── hooks/               # useChatInit / useChatActions / useStreaming / usePlanMode
│                        # usePageConfig / useDelayedFlag / useStallDetector
├── stores/              # 18 Zustand stores (authStore, chatStore, catalogStore,
│                        # editionStore, uiStore, settingsStore, fileStore, kbStore,
│                        # agentStore, automationStore, batchStore, canvasStore,
│                        # mySpaceStore, projectStore, skillDistillStore, …) + index.ts
├── utils/               # citations / markdown / segments / constants / apiError / adminApi …
└── styles/              # CSS split by feature area (variables.css is the variable source of truth)
```

Naming conventions:

- **Components**: PascalCase filenames, one component per file, **named export** (`export function Xxx()`), Props interfaces end in `Props`; new components must be exported from their group's `index.ts`.
- **Stores**: `xxxStore.ts`, `export const useXxxStore = create<XxxState>(...)`; state and actions live in the same interface; re-exported from `stores/index.ts`.
- **Hooks**: `useXxx.ts` under `hooks/`, barrel-exported from `hooks/index.ts`.
- **Styles**: custom class names always use the `.jx-` prefix (BEM: element `-`, modifier `--`); colors / radii / spacing must use the CSS variables in `styles/variables.css` — no hard-coded color values.
- **Types**: anything shared across components goes in `types.ts`; component-private Props stay in the component file; avoid `any`.

## API call conventions

All calls go through `api.ts`; paths are `getApiUrl()` (`VITE_API_BASE_URL`, default `/api`) + `/v1/...`. The backend returns a unified envelope, which `api.ts` unwraps internally:

```ts
interface ApiEnvelope<T> {
  code: number;
  message: string;
  data: T;
  trace_id?: string;
  timestamp?: number;
}

// inside api.ts: isApiEnvelope() type guard + unwrapData() extracts data
```

Steps for a new API call:

1. Define the request/response types in `types.ts` (shared) or `api.ts` (API-layer only);
2. Add an exported function in `api.ts` that goes through the shared request wrapper (auth header, 401 triggers the login flow, envelope unwrapping) and returns the unwrapped `data` — model it on an existing function such as `getEditionInfo()`;
3. Components and stores call `api.ts` functions only — **no** scattered raw `fetch` calls;
4. Console pages (/admin, /config) use `adminFetch` / `configFetch` from `utils/adminApi.ts` (the token is passed down by the shell component).

Error handling is centralized in `utils/apiError.ts`: `readErrorMessage()` parses the envelope/detail; HTTP 402 (feature not licensed) is identified by the `LicenseError` **type** (not by message substring), and `licenseErrorMessage()` appends guidance to activate a license.

## Adding a component group / store

**New component group**:

1. `mkdir src/frontend/src/components/<group>/`, write the components plus an `index.ts` barrel;
2. Styles go in `styles/<group>.css` (`.jx-` prefix + CSS variables), imported in `styles/index.ts`;
3. Re-export from `components/index.ts` where that file aggregates.

**New store**:

1. Create `stores/xxxStore.ts`: define `interface XxxState` (state + actions) first, then `create<XxxState>((set, get) => ({...}))`;
2. Add the export to `stores/index.ts`;
3. Wrap async actions in try/catch with `set({ loading })`; persistable state writes localStorage in its setter (key naming follows the `hugagent_ui_*` style, see `storage.ts`);
4. Subscribe in components via the hook form (prefer selectors: `useXxxStore((s) => s.field)`); read latest values inside event callbacks with `useXxxStore.getState()`.

## Edition gating (CE/EE)

The frontend hides EE sections according to the deployed edition and license feature bits. The mechanism (`stores/editionStore.ts`):

- The app calls `fetchEdition()` once at startup (`App.tsx`), hitting the unauthenticated probe `GET /v1/meta/edition` for `edition` (ce/ee), `mode` (license state machine), and `features` (boolean feature map);
- **Before the probe returns, the UI is optimistically permissive** (features treated as all-true) so EE deployments never flicker; probe failures also silently keep the full UI — the backend 402 gate remains the backstop;
- After the probe returns, the bits tighten: all-false on CE, per-entitlement on EE.

Read a feature bit in a component with the **reactive selector** (the standard `loaded`-aware form):

```ts
import { useEditionStore } from '../../stores';

// multi-tenancy bit: optimistic true while loading, actual value afterwards
const multiTenancy = useEditionStore((s) => (s.loaded ? !!s.features.multi_tenancy : true));

// usage: hide EE sections on CE / unlicensed deployments
{multiTenancy && <TeamsSection />}
```

Existing in-repo examples:

- `components/settings/SettingsModal.tsx` — when `multi_tenancy=false`, the "Teams" section is filtered out of the settings navigation entirely;
- `components/myspace/MySpacePanel.tsx` — hides the team-folder tab and falls back to the personal space.

Caveats:

- Frontend hiding is **purely a UX improvement**; the security boundary is the backend `requires_feature` guard (402) — never treat frontend hiding as access control;
- When adding new EE UI: ① gate it with the selector on the matching feature bit; ② if the entire page is EE (like /admin, /config), no runtime gating is needed — the CE derived tree physically excludes the component directories and removes the routes via the overlay `main.tsx` (see [CE Build Pipeline](../editions/build-ce.md));
- When handling API errors, branch on `LicenseError` (402) and show "contact your administrator to update the license" rather than a generic failure message.

## New-feature checklist

- [ ] Components are PascalCase + named export + Props interface
- [ ] Global state goes through Zustand; the store is exported from `stores/index.ts`
- [ ] API calls go through `api.ts` / `adminApi.ts`, fully typed, no `any`
- [ ] Class names use the `.jx-` prefix; colors / spacing / radii use CSS variables
- [ ] EE sections gated by feature bit (or confirmed to be within the physical-exclusion scope)
- [ ] 402 errors take the `LicenseError` branch
- [ ] `npm run lint` passes; verified via rebuild or hot-swap

## Related source

| Topic | Path |
|---|---|
| API client / envelope unwrapping | `src/frontend/src/api.ts` |
| Shared types | `src/frontend/src/types.ts` |
| Error / 402 detection | `src/frontend/src/utils/apiError.ts` |
| Console requests | `src/frontend/src/utils/adminApi.ts` |
| Edition gating store | `src/frontend/src/stores/editionStore.ts` |
| Gating usage examples | `src/frontend/src/components/settings/SettingsModal.tsx`, `src/frontend/src/components/myspace/MySpacePanel.tsx` |
| CSS variables | `src/frontend/src/styles/variables.css` |
| Entry (CE divergence point) | `src/frontend/src/main.tsx`, `ce/overlay/src/frontend/src/main.tsx` |

See also: [Backend Development Guide](backend.md) · [Frontend Architecture](../architecture/frontend.md) · [License Mechanism](../editions/license.md)
