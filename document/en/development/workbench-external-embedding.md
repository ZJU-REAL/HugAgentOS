# Embedded external pages

The administration console is an EE feature. Select Embedded workspace and configure the exact origin, readiness mode, destination storage and form permission. Ordinary external links still open in a browser.

Example:

```json
{
  "declaration": {
    "schemaVersion": "1.0.0",
    "id": "reports",
    "title": "Reports",
    "target": {"kind": "external", "mode": "embedded", "url": "https://reports.example.org/home"}
  },
  "enabled": true,
  "embedding": {
    "origin": "https://reports.example.org",
    "ready": "integrated",
    "originStorage": true,
    "allowForms": false
  }
}
```

Updates also require the current revision. The origin must equal the destination's canonical origin. Paths, wildcards and the host hostname are rejected. Changing the port or scheme still shares hostname-scoped cookies. embedding is an administrator policy, not a plugin permission request. Browser mode cannot carry embedding grants.

Ordinary pages use ready=unconfirmed and default to originStorage=false. Their opaque iframe permits scripts only. After eight seconds the host reports unconfirmed readiness. A load event cannot establish application readiness or prove that embedding was refused.

Integrated pages require explicit trust in a separate origin and originStorage=true. Host HTML must send an HTTP Content-Security-Policy response header with frame-ancestors 'self' or stricter 'none'. Without this deployment protection, named-origin storage and integrated readiness fail closed and a browser alternative remains available. Each runtime must apply the same protection; do not disable the check for compatibility.

The intermediary carrier runs fixed code with a random CSP nonce and an exact-origin frame-src policy. Destination redirects cannot navigate to another origin or the host. Host ancestor protection prevents external pages from nesting host documents. The destination sandbox adds allow-same-origin or allow-forms only when approved. Popups, downloads, top navigation and device access stay disabled. External pages receive no plugin SDK, host storage, credentials, unrestricted navigation or native invoke.

After receiving hugagent.external.init, an integrated page must validate the parent's source and exact host origin. Once its business initialization finishes, it returns the same nonce:

```js
window.addEventListener('message', event => {
  if (event.source !== parent || event.origin !== 'https://host.example.org') return;
  const input = event.data;
  if (input?.type !== 'hugagent.external.init' || input.protocolVersion !== '1.0.0') return;
  parent.postMessage({
    type: 'hugagent.external.ready',
    protocolVersion: '1.0.0',
    nonce: input.nonce
  }, event.origin);
});
```

The carrier checks source, exact origin, protocol version and a fresh nonce for each load. Readiness confirms participation by the configured origin, not the correctness of its business data. Old pages, wrong nonces, other windows and ordinary pages cannot claim readiness. Retry, close and identity changes release old ports and timers.

Open in browser always remains available. Its click requests a new authenticated launch, preserves user activation, removes opener and suppresses Referrer. Failures produce feedback. Never remove destination CSP/X-Frame-Options, proxy third-party content or copy host credentials. Destination cookies, shared identity and exchange use the separate authentication integration rules.

Browser references: [iframe](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/iframe), [frame-src](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-src), [frame-ancestors](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/frame-ancestors).
