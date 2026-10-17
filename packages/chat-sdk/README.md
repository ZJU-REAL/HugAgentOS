# HugAgentOS Chat SDK

Version 1.0.0. This package has no dependency on the platform application source.

- `@hugagent/chat-sdk`: browser/Node fetch client, public HTTP types and SSE framing.
- `@hugagent/chat-sdk/react`: React 19 runtime views/actions and customer component contract, API version 1.

Build with `npm ci && npm run build`. Repository acceptance tests also require `npm ci` in src/frontend first. Run `npm test` for protocol and installed-consumer acceptance.
Create a distributable with `npm pack --pack-destination <directory>`. Install that tarball in the customer repository and commit the customer lockfile.

## Independent frontend

```ts
import { ChatClient } from '@hugagent/chat-sdk';
const client = new ChatClient({ baseUrl: '/api' });
const chat = await client.createChat({ title: 'New chat' });
for await (const event of client.send({ chat_id: chat.chat_id, message: 'Hello' })) {
  // Persist run_id and event_offset. Render the typed event and its extra fields.
}
```

The base URL includes /api behind the standard nginx gateway. Use same-origin session cookies by default.
For other deployments, configure the platform authentication adapter and CORS/CSRF policy on the server.
A rotating `headers` callback can supply the authenticated user's credential. Never put an administrator token or shared secret in frontend code.
401/403 and business-envelope errors throw `ChatApiError`, preserving HTTP status, business code and detail.
Upload is a documented bare-JSON endpoint; other JSON operations require the platform envelope.

`readSSE` recognizes LF/CRLF, multiline data, UTF-8 boundaries, heartbeats, [DONE], end/run_terminal, and terminal run snapshots.
Unfinished EOF throws: closing a subscription does not cancel the backend task.
Store run_id and event_offset; call `resume(runId, offset)` to replay events, or `subscribe(runId)` for the native snapshot subscription.
An AbortSignal stops local transport only. Call `cancel(runId)` to cancel the backend run.
Unknown event types pass through. The client does not interpret domain events, retry a sent message, or automatically grant tool permissions.
Consumers must render `file_confirm`, `design_pick`, `user_question`, expiry/resolution, tool states and any product-specific events they enable.
A native snapshot contains the authoritative blocks/tools/signals state; it replaces the prior projection.
The independent example demonstrates text and confirmation rendering; retain the default message children for the product's rich citations, artifacts and other domain renderers.

History is paginated, default ascending. Load all pages or implement a paged history UI; page 1 is not the latest 50 messages.
`messages` page size is 1–100. A single decoded SSE frame is limited to 4 MiB.
`pendingConfirms` and `pendingQuestions` recover human interaction gates after a reload.
`confirmFile` returns stale/interrupted information; report it to the user. Question answers use wire fields id/selected/custom/skipped.

## Host customization

Export `defineChatCustomization({ apiVersion: 1, id: 'customer-name', slots: {...} })` from a trusted customer module.
Slots are welcome/composer/message/sidebar/conversation; each receives default children.
Use `useChatRuntime()` to read immutable copied views and invoke platform actions. No internal store, controller or relative host-source imports are supported.
The message slot also receives its immutable message view. Compose default children to preserve rich product controls.
The composer contract exposes draft, attachment state, attach/retry/remove, send/cancel, and confirmation/question actions.
Confirmation actions preserve server stale responses. Successful question POSTs are authoritative terminal decisions and remove the pending prompt.
More specialized platform workflows remain available through the default children.

Set HUGAGENT_CHAT_CUSTOMIZATION at build time to the customer module/package. This never loads arbitrary code chosen by a runtime URL or user.
The application validates the API version and displays a visible startup error on an incompatible contract.
Public shared-chat pages remain on the standard renderer and do not receive an authenticated host runtime.
Customer CSS must be scoped below [data-chat-customization="customer-name"] and use host theme tokens.
Customer components are trusted application code and have application permissions; this is not a sandbox for third-party JavaScript.

## Compatibility

Pin the SDK version and backend release in each customer deployment. The initial SDK targets the /v1 contracts in this repository release.
Added event fields/types are forward-compatible; removing or changing a public field requires a major SDK/UI-contract version and migration.
A new official interface does not automatically change a customer's pinned interface.
Updating shared packages preserves customer source; deployments still require compatibility testing of customer capabilities.

## Independent developer preview

The optional @hugagent/chat-sdk/preview entry exports PreviewHost. It renders a customer customization with the public runtime and all five slots. It uses local sample state and never calls a backend. It is a contract harness, not the complete product renderer. Import React in the consuming application and pass its customization to PreviewHost. The independent developer kit includes the editable preview entry, a real API chat example, backend login callback, and a fixed-upstream development proxy.
