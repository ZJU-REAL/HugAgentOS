# SDK API 1.0.0

## Connection

```js
import {connect} from '@hugagent/workbench-sdk';
const connection = await connect({requiredCapabilities: ['host.info']});
try {
  const info = await connection.request('host.info', {}, signal);
  document.documentElement.lang = info.locale;
} finally {
  connection.dispose();
}
```

`connect({timeoutMs?, requiredCapabilities?})` resolves after the version handshake.
The maximum handshake duration is eight seconds. Missing required capabilities reject with `host_incompatible`.
`Connection.info` contains protocolVersion, theme, locale, context and negotiated capabilities.
Context contains applicationKey, installId and packageRevision.
Navigation-capable hosts also send route `{path, requestId}`.
`onUpdate(listener)` returns an unsubscribe function. Listeners render updates; they must not echo navigation requests.
`dispose()` closes the channel and rejects pending calls.
`request<T>(method, params?, signal?)` returns a Promise. Use AbortSignal for cancellation.
A request has a ten-second timeout. There are eight concurrent requests and a 256 KiB message limit.
The host applies its request rate limit and checks actual ownership and authorization.

## Methods

Only use methods present in the negotiated capability set.
Declaring a module grant requests approval; it never grants authority.

| Method | Request | Response / boundary |
| --- | --- | --- |
| host.info | {} | Minimum HostInfo; no tokens or conversation body |
| workspace.navigate | {path, replace?, requestId} | {path, requestId}; current application's declared view |
| navigation.open | {applicationKey, path?} | {opened}; exact catalog key, host confirmation |
| data.query | {source, params} | Declared source response; module needs data.query and data_source:<source> |
| file.pick | {accept?} | {fileId,name,size,mimeType?} or null; real host selection |
| file.save | {fileId,name?} | {saved}; selected owned file, real host confirmation |
| chat.compose | {text} | {composed}; host confirmation, no automatic send |

The workspace example calls request directly. Optional SDK helpers wrap the same wire methods.
Additional state, lifecycle, resource, badge and platform methods depend on the host's negotiated extensions.
Unsupported methods fail explicitly. The preview simulates only methods in the table except navigation.open and chat.compose.
It must not emulate production secrets, business writes or native permission.

## Errors

Promise rejection messages are machine-readable codes.
Do not retry operations with side effects automatically.

| Code | Action |
| --- | --- |
| bridge_timeout | Offer explicit retry after checking host compatibility |
| host_incompatible | Update the host or remove an unsupported required capability |
| session_closed | Stop work; open a fresh authorized instance |
| cancelled | Stop pending UI work; retain a user's draft |
| unknown_method | Disable the unsupported action |
| capability_denied / permission_denied | Request administrator review; do not bypass the host |
| data_source_denied | Check the declared source ID and its approved grant |
| file_access_denied | Ask the user to select an owned file again |
| invalid_params / invalid_route | Correct the request; remain inside the application |
| concurrency_limit / rate_limited | Stop excess calls; use a bounded user-driven retry |
| message_too_large | Reduce the message; use the file-reference path |

A revoked installation, account change or host logout invalidates old channels.
Never treat a plugin-supplied gesture flag as authorization.
