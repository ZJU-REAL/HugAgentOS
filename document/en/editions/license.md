# License Mechanism (Enterprise Edition)
> Last updated: 2026-06-11

The Enterprise Edition (EE) uses a **GitLab-style offline license model**: a single Ed25519-signed authorization file (`.lic`) verified in-process — **fully offline, no license server** — designed for air-gapped environments such as government intranets. This page documents the state machine, enforcement, issuance flow, and management UI; everything here is verified against the code in `src/backend/core/licensing/`.

## License file format

A `.lic` file is a JSON envelope (`src/backend/core/licensing/_ee_verify.py`, format version `jx-license/1`):

```json
{
  "format": "jx-license/1",
  "payload": "<base64(payload JSON bytes)>",
  "signature": "<base64(Ed25519 signature over the payload bytes)>"
}
```

Payload fields:

```json
{
  "license_id": "lic_xxx",
  "customer": "Customer name",
  "edition": "ee",
  "features": ["*"],            // "*" = everything; or ["sso", "billing", ...]
  "seats": 0,                   // 0 = unlimited seats
  "issued": "2026-06-10",
  "expires": "2027-06-10"
}
```

The verification public key is built into `_ee_verify.py` (`_BUILTIN_PUBKEY`) and can be overridden via the `LICENSE_PUBLIC_KEY` environment variable (for key rotation). `_ee_verify.py` is an **EE-only module that never enters the CE derived tree** (explicitly excluded in `ce/manifest.yaml`; the CE tree's `manager.py` is replaced by an always-False stub via overlay).

## State machine

`core/licensing/manager.py::LicenseManager.mode()` returns one of seven states:

| mode | Trigger | EE feature bits |
|---|---|---|
| `ce` | `JX_EDITION=ce` (community tree) | all False |
| `internal` | ee, no license file configured, and `JX_LICENSE_REQUIRED=false` | **all True** — internal / fully-managed deployments; backward-compatible with all existing installations |
| `licensed` | license verifies and is within its validity window | per entitlement (`features` list or `"*"` wildcard) |
| `grace` | expired but within the grace window (`LICENSE_GRACE_DAYS`, default 14 days) | features retained; probe / console raise warnings |
| `expired` | past the grace window | all False (the app itself keeps working; org-level capabilities degrade) |
| `invalid` | `LICENSE_KEY_PATH` is configured but the file is missing / unreadable / fails verification / malformed | all False |
| `missing` | `JX_LICENSE_REQUIRED=true` and no license file | all False |

Key design points (all verifiable in `manager.py`):

- **`invalid` is strictly distinguished from "not configured"**: a configured path with an unreadable file must classify as `invalid` and must never fall back to `internal` — otherwise deleting an expired license file would restore full functionality.
- **Single source of truth for validity classification**: `classify_verified()` is shared by the runtime state machine and the upload validation, guaranteeing that "can this file be activated" and "is it honored at runtime" can never diverge.
- **mtime caching**: verification results are cached by file mtime; `reload()` clears the cache for hot-swapping.

### Related environment variables

| Variable | Default | Purpose |
|---|---|---|
| `JX_EDITION` | `ee` (main repo) | Edition shape |
| `LICENSE_KEY_PATH` | empty | Path to the license file (in-container path; mount a persistent volume) |
| `JX_LICENSE_REQUIRED` | `false` | **Enforced mode**: when true, all EE feature bits are off without a valid license (recommended for private delivery); when false with no license = internal full-feature mode |
| `LICENSE_GRACE_DAYS` | `14` | Grace window after expiry, in days |
| `LICENSE_PUBLIC_KEY` | empty (built-in key) | Public-key override (key rotation) |

## Feature bits and enforcement

### The Feature enum

`core/licensing/features.py::Feature` lists only **organization-level** commercial bits (automation / batch / personal canvas / L2–L3 memory belong to CE and are deliberately absent):

`sso`, `multi_tenancy`, `audit`, `memory_audit`, `billing`, `quota`, `persistent_sandbox`, `cloud_storage`, `industry_tools`, `content_admin`, `system_config`, `canvas_collab`, `whitelabel`.

### Two lines of defense

1. **First line: the router registry** — the CE tree physically lacks EE route files. See [CE Build Pipeline](build-ce.md).
2. **Second line: the `requires_feature` guard** (`core/licensing/deps.py`) — protects against "EE code is fully deployed but the license does not include a given capability pack".

The mapping between EE routes and feature bits is declared in the registry `src/backend/api/routes/v1/__init__.py::EE_ROUTERS` (the third tuple element is the feature bit); `api/app.py` attaches guards from the table at registration time:

| EE route module | Feature bit |
|---|---|
| `audit`, `admin_chat_history`, `admin_logs` | `audit` |
| `admin_skills`, `admin_kb`, `admin_prompts`, `admin_mcp_servers`, `admin_agents`, `admin_skill_drafts`, `admin_sandbox`, `admin_marketplace` | `content_admin` |
| `admin_usage_logs`, `admin_billing` | `billing` |
| `config_users`, `config_teams`, `config_invites`, `team_files` | `multi_tenancy` |
| `config_security`, `service_configs` | `system_config` |
| `config_verify`, `config_license`, `auth` | **None (explicit exemption)** |

The three exemptions are deliberate: `config_verify` is the console login check, `config_license` is the entry point for swapping licenses, and `auth` is login/session infrastructure — all of these must remain reachable when the license is invalid, otherwise users are trapped in a "402 → logout → login → 402" loop with no way to replace the license. The SSO bit is not blanket-exempted at the router level; it guards itself: the authorize-url endpoint carries `requires_feature(Feature.SSO)` (`api/routes/v1/auth.py`), and remote ticket exchange checks inside `core/auth/sso.py::exchange_ticket`.

> Note: `quota` / `persistent_sandbox` / `cloud_storage` / `industry_tools` / `canvas_collab` / `whitelabel` / `memory_audit` are currently expressed in license entitlements and the probe but have **no router-level guard attached** — those boundaries are enforced mainly by physical exclusion from the CE tree and by deployment configuration.

### Unauthorized requests return 402

An unauthorized access raises `FeatureNotLicensed` (`features.py`), rendered by the global error handler as an HTTP **402** envelope — this is the **single** source of license 402s:

```json
{ "code": 40201, "message": "Feature not licensed: xxx", "data": { "feature": "xxx", "mode": "expired" } }
```

402 was chosen over 403 because the frontend treats 403 as token expiry and forces logout. Seat-related rejections use `SeatLimitExceeded` (code `40202`, also 402). On the frontend, `src/frontend/src/utils/apiError.ts` identifies 402s by the `LicenseError` type and appends guidance to activate a license under System Config → License.

## Seat limits

Seat counting has a single source of truth in `core/licensing/seats.py`:

- `seats_used(db)`: seats in use = the full row count of `users_shadow` (including SSO shadow accounts);
- `seat_available(db)`: the check run before creating any user (shared by local sign-up and SSO auto-provisioning). Always allowed in CE / internal / unlimited (`seats=0`); under `licensed` / `grace` it requires `active_users < seats`; always denied under `expired` / `invalid` / `missing`;
- `seat_block_reason(db)`: the rejection message distinguishes the two root causes — a genuine seat shortage (`licensed`/`grace`) suggests expansion, while an unhealthy license state points to the License panel.

## Status query and hot-swap

### `GET /v1/meta/edition` (unauthenticated probe)

`api/routes/v1/meta.py`: returns `edition` / `mode` / the boolean feature map. It **deliberately omits** license details (license_id / customer / seats / expiry) — those are only exposed on authenticated endpoints. `mode` is kept so login pages can show hints such as "license expired".

### `GET/POST /v1/config/license` (CONFIG_TOKEN auth)

`api/routes/v1/config_license.py`:

- `GET`: full status (`license_manager.status()` + `seats_used`), including live per-feature evaluation, grace days, and license metadata;
- `POST`: upload the full `.lic` text (≤64 KB) to hot-swap. Flow: **verify before writing** (an invalid file never overwrites the current license) → reject activation of licenses past the grace window (within grace it is allowed, so a lost file / rebuilt host can re-attach the same license during the window) → atomic write to `LICENSE_KEY_PATH` (tmp file + `os.replace`) → `license_manager.reload()` takes effect immediately, **no restart**. Returns 400 if `LICENSE_KEY_PATH` is not configured.

### The /config console License panel

`src/frontend/src/components/config/LicensePanel.tsx` (mounted in `ConfigApp.tsx`): shows edition / mode (each of the 7 states has a colored tag and remediation hint), license details (customer, expiry, seat usage `seats_used/seats`), the localized list of all 13 feature bits with their on/off state, plus an "upload license" modal (paste the `.lic` text) and refresh.

### Frontend edition gating

`src/frontend/src/stores/editionStore.ts`: the app fetches the probe at startup; **before the probe returns, the UI is optimistically permissive** (features treated as all-true so EE deployments do not flicker), then tightens to the actual bits. Components use a reactive selector:

```ts
const multiTenancy = useEditionStore((s) => (s.loaded ? !!s.features.multi_tenancy : true));
```

Usage examples: `components/settings/SettingsModal.tsx` (hides the Teams section), `components/myspace/MySpacePanel.tsx` (hides the team-folder tab). Frontend hiding is purely a UX nicety — the backend 402 guard is always the backstop.

## Issuance flow (vendor side)

`scripts/license_tool.py` is the vendor's offline issuance tool and is **never shipped in any distribution** (explicitly excluded in the CE manifest). Four subcommands:

```bash
# 1. Generate an Ed25519 keypair (one-time; keep the private key offline —
#    a leak allows arbitrary issuance)
python scripts/license_tool.py keygen --out-dir ~/jx-license-keys
#    The printed public key goes into core/licensing/_ee_verify.py::_BUILTIN_PUBKEY
#    (or LICENSE_PUBLIC_KEY on the customer side)

# 2. Issue
python scripts/license_tool.py issue \
    --key ~/jx-license-keys/license_signing.key \
    --customer "Customer name" \
    --expires 2027-06-10 --seats 200 --features "*" \
    --out customer.lic

# 3. Inspect the payload (no verification — readable even with a bad signature)
python scripts/license_tool.py inspect customer.lic

# 4. Verify + status
python scripts/license_tool.py verify customer.lic --pub ~/jx-license-keys/license_signing.pub
```

`issue` auto-generates `license_id` (`lic_` + 16 hex chars) and validates date formats; `--seats 0` means unlimited; `--features` is a comma-separated bit list, `"*"` for everything. The envelope format and verification logic have a single source of truth in the backend's `_ee_verify.py`, which the tool reuses directly (passing the public key explicitly to avoid pulling in the backend settings chain).

## Private-delivery checklist

1. Configure `LICENSE_KEY_PATH=/app/data/license.lic` (persistent volume) and `JX_LICENSE_REQUIRED=true` in `.env`;
2. After deployment, open `/config` → License panel and paste the `.lic` content to activate;
3. Verify: `GET /v1/meta/edition` should report `mode: licensed`;
4. Renewal: issue a new file before expiry and upload it via the panel (functionality is uninterrupted within the grace window).

## Related source

| Topic | Path |
|---|---|
| State machine / facade | `src/backend/core/licensing/manager.py` |
| Ed25519 verification (EE-only) | `src/backend/core/licensing/_ee_verify.py` |
| Feature enum + 402 exceptions | `src/backend/core/licensing/features.py` |
| `requires_feature` guard | `src/backend/core/licensing/deps.py` |
| Seat counting | `src/backend/core/licensing/seats.py` |
| EE route ↔ feature registry | `src/backend/api/routes/v1/__init__.py` |
| Guard attachment | `src/backend/api/app.py` (edition registration loops) |
| Status query / hot-swap | `src/backend/api/routes/v1/config_license.py` |
| Probe | `src/backend/api/routes/v1/meta.py` |
| Issuance tool | `scripts/license_tool.py` |
| License panel | `src/frontend/src/components/config/LicensePanel.tsx` |
| Frontend gating / 402 detection | `src/frontend/src/stores/editionStore.ts`, `src/frontend/src/utils/apiError.ts` |

See also: [Community vs. Enterprise Edition](overview.md) · [CE Build Pipeline](build-ce.md) · [Environment Variables](../deployment/environment-variables.md)
