# Workbench SDK and plugin pages

SDK 1.0.0 uses UI v3 and Workbench protocol 1.0.0. The independently packaged SDK remains private / UNLICENSED. It is not automatically published to npm.

## Build a page

Build packages/workbench-protocol first. Run npm ci, npm test and npm pack in packages/workbench-sdk. Install the SDK tarball into an independent plugin project. Plugins reference the SDK without host source, React, stores or native desktop APIs.

```js
import {connect} from '@hugagent/workbench-sdk';
const connection = await connect();
const info = await connection.request('host.info');
document.querySelector('p').textContent = info.locale;
connection.onUpdate(info => {
  document.documentElement.dataset.theme = info.theme;
});
window.addEventListener('unload', () => connection.dispose());
```

Bundle the SDK and JavaScript dependencies into each entry script. Disable code splitting and dynamic imports. HTML may reference separate package-local entry scripts, CSS and images. The server embeds these static assets into the isolated page. Expand CSS imports and srcset during plugin compilation. Expanded pages have an 8 MiB limit. Oversized pages fail explicitly. Remote resources and CDNs do not load.

```json
{
  "name": "reports",
  "version": "1.0.0",
  "extensions": {"org.hugagent": {"ui": {
    "version": 3,
    "contributes": {
      "modules": [{"id": "main", "surface": "workspace", "entry": "web/index.html", "grants": []}],
      "applications": [{"schemaVersion": "1.0.0", "id": "reports", "title": "Reports", "target": {"kind": "plugin", "moduleId": "main"}}]
    }
  }}}
}
```

Keep plugin.json and web/ in the ZIP. UI-only plugins need no skill or MCP component. Use the existing plugin import flow, then open the page from App Center. Unopened pages create no iframe. Closing, disabling or switching accounts removes the page and releases its bridge. UI v1/v2 tool results and canvas retain their existing protocol.

## Handshake and authorization

The SDK uses a per-document nonce and MessageChannel. Handshake retries are bounded by eight seconds. Failure affects only the page, which offers retry. Nonces use getRandomValues and support insecure HTTP and opaque frames.

Capabilities depend on the installed host extensions. Required capabilities must be negotiated before readiness. Unknown request methods return unknown_method. Messages have a 256 KiB limit, eight concurrent requests and 30 requests per ten seconds. Requests cancel after ten seconds. AbortSignal and dispose cancel pending calls.

Context contains only applicationKey, installId and packageRevision. Handshake and update messages carry host theme and locale. Credentials, conversation text and native permissions are excluded. Language changes retain the host reload behavior.

Permission selectors use the full plugin applicationKey. Matching an external application ID grants no plugin access. Directory, launch and every asset fetch check the user, installation, version and enabled status. Existing allowed_apps null and force-all inheritance semantics remain.

Use the [developer preview](workbench-preview.md) for standalone examples and permission simulation. Real-host acceptance is separate. See [administration](workbench-administration.md), [errors](workbench-errors.md) and [migration](workbench-migration.md).

Application keys are opaque references. They combine a digest of the full installation binding with its UTC creation instant, including microseconds. Use the exact catalog key for navigation preferences and permission selectors. Upgrades retain that key; uninstalling and reinstalling changes it. Old launch and package URLs then fail authorization.
