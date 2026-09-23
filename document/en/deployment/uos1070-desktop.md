# UOS 1070 aarch64 Desktop Client

> [简体中文](../../zh-CN/deployment/uos1070-desktop.md)

UOS 1070 aarch64 uses the Electron 43 release line in `desktop-uos/`. The supported
delivery format is a Debian ARM64 `.deb`. It does not depend on system WebKitGTK 4.1,
so it avoids the Tauri 2 WebView dependency blocker on the Debian 10-era UOS base.

## Support baseline

| Item | Production baseline |
|---|---|
| Operating system | UOS 1070 Desktop, aarch64 |
| libc | glibc 2.28 |
| Desktop shell | Electron 43.4.1 |
| Installer | Debian `arm64` `.deb` |
| Local runtime | Private CPython 3.11 with an `aarch64-manylinux_2_28` lock |
| Data | `~/.hugagent` |
| Configuration | `~/.config/com.hugagent.desktop` |

The full package supports local, cloud, and dual modes with the existing desktop
capabilities: deep-link login, loopback streaming proxy, tray/quick ask, native
notifications, local-folder projects, dual-upstream routing, identity/model bridges,
offline local-service install/upgrade/rollback, and SHA-256 verified updates.

The UOS window follows the compact macOS client layout: it keeps the native title bar
and window controls. Its File menu offers New Window (Ctrl+Shift+N), New Chat,
and Quit; quick ask keeps its compact appearance without a menu bar.

A distribution brand may preset the first-launch mode for a full package. A fixed
dual-mode build goes directly to one-action initialization. The initialization and
installation pages use the same halo, orbit, and progress-highlight motion as the
existing desktop client, while showing only the current stage, percentage, and
actionable errors. Raw logs remain in the local-service log directory for diagnosis
instead of appearing at the bottom of the page.

## Build and verification

Build the full package on native Linux aarch64, preferably UOS 1070 itself. An x86_64
builder may only cross-package the remote-only shell without the Python runtime.

```bash
cd desktop-uos
npm install
npm test
npm run build
npm run verify:deb -- "dist/HugAgentOS UOS_0.5.15_uos1070_arm64.deb"
```

Release acceptance must cover:

1. Debian architecture is `arm64`;
2. the Electron ELF imports no GLIBC symbol newer than 2.28;
3. a disconnected full-package cold start performs no pip, uv, or Python download;
4. local, cloud, and dual modes pass login, SSE chat, file transfer, folder selection, and notifications;
5. dual-mode cloud projects remain in the cloud while local-folder projects remain local;
6. a staged update passes SHA-256 verification, system authorization, installation, and restart.

UOS shares one release index with Windows and macOS: each client fetches the update for its
own platform and reports "no update available for this platform" when none was published,
while older clients that send no platform read the common release, which advances only once
all three platforms have published that version. The desktop version is identical across the
three platforms and the build enforces it.

See [`desktop-uos/README_EN.md`](../../../desktop-uos/README_EN.md) for commands,
artifacts, and publishing.

> Electron 43 upstream support is scheduled to end in January 2027. Treat it as the
> current UOS compatibility baseline, keep the latest 43.x patch deployed, and qualify
> Electron 44+ continuously.

## Parity with the current Tauri client

UOS shares the current React frontend and CE backend sources. Rebuilding the full package includes
the latest capability origins, local enablement, skill sync and working-directory behavior.
Updating the cloud alone does not replace resources in an installed client.

- File supports opening a folder into a local project conversation; ordinary packages can change runtime mode.
- View offers Zoom In, Zoom Out and Actual Size (Ctrl +/-/0 or Ctrl+wheel). Tauri's zoom steps are
  saved in `prefs.json` and reused by new windows and after restart; system DPI remains native.
- Help, tray and sidebar share update handling. Background release notices only offer an update;
  confirmation opens download progress with cancellation during download. Installation still requires
  Ed25519 signature and SHA-256 verification and UOS system authorization.
  Older servers use low-frequency checks; reconnects back off and concurrent checks cannot install twice.
- The loopback proxy checks Origin/Fetch Metadata before injecting session and bridge credentials;
  direct requests without source metadata are refused.
- Service logs rotate while running, keeping the current and previous files at up to 32 MiB each.
  Capability readiness checks back off from 500 ms to 15 seconds. Existing UOS data and capability
  directories are preserved without automatically migrating configuration.

Full packages also support Tauri's hybrid-only build option:

```bash
JX_DEFAULT_SERVER_BASE=https://agent.example.com npm run build:hybrid-only
```

This fixes the cloud address and dual mode and requires a cloud address. Thin packages cannot be
hybrid-only. Full offline ARM64 packages still require native aarch64 build and installation validation.
