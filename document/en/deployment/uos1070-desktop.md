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

See [`desktop-uos/README_EN.md`](../../../desktop-uos/README_EN.md) for commands,
artifacts, and publishing.

> Electron 43 upstream support is scheduled to end in January 2027. Treat it as the
> current UOS compatibility baseline, keep the latest 43.x patch deployed, and qualify
> Electron 44+ continuously.
