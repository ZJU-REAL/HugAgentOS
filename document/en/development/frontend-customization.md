# Customer chat customization

HugAgentOS provides three extension levels.

| Scope | Interface | Upgrade ownership |
| --- | --- | --- |
| Brand, text, suggestions and navigation | Existing page configuration | Preserve deployment configuration data |
| Welcome, composer, message, sidebar and conversation layout | Customer component package | Customer repository pins and builds its SDK dependency |
| Existing portal integration or a new frontend | HTTP/SSE chat SDK | Release the frontend separately and test backend compatibility |

## Customer components

Import only from `@hugagent/chat-sdk/react`. Do not import host stores, controllers or internal component paths.
The public contract provides copied immutable views, actions and five component slots.
Each slot receives the default content as children. Compose it to retain rich artifact previews, citations and product controls.

Export a default `defineChatCustomization({ apiVersion: 1, id: 'customer-portal', slots: {...} })` object.
Slots are welcome/composer/message/sidebar/conversation; message also receives its own message view.
The runtime provides draft, attachment upload/retry/removal, send/cancel, regeneration, edit/resend, confirmations and question answers.
Specialized plan, canvas and domain interactions can remain in the default children.
Scope CSS below `[data-chat-customization="customer-portal"]` and use the host theme tokens.
Customer modules are trusted application code. Runtime URL parameters cannot choose executable code.

Build the source example:

```sh
cd src/frontend
npm ci
HUGAGENT_CHAT_CUSTOMIZATION=../../examples/customer-chat/src/customization.tsx npm run build
```

Without the variable, the official interface is built. Missing modules fail the build; incompatible API versions display a visible error.
Public shared-chat pages retain the standard renderer and do not receive the authenticated host runtime.

## Independent repositories

Run npm ci and npm pack in packages/chat-sdk. Deliver that tarball to the customer repository.
Copy examples/customer-chat and replace its relative file dependency with the pinned SDK tarball or private-registry version.
Run npm install, npm run build and npm pack. The resulting customer package includes JavaScript, declarations and CSS.
Install it into the platform frontend and set HUGAGENT_CHAT_CUSTOMIZATION to its package name.

The packed SDK does not depend on host source. Keep customer sources and lockfiles in the customer repository.
Platform upgrades must not copy over that directory. Record backend, SDK, customer package and frontend image versions separately.
Major interface changes require migration and acceptance testing.

## Independent frontend

`@hugagent/chat-sdk` provides the HTTP client without React.
Run npm run standalone in the customer example to produce static assets.
VITE_CUSTOMER_API_BASE and VITE_CUSTOMER_LOGIN_URL select the API and normal platform login entry at build time.
A same-origin gateway can reuse the platform session. Configure authentication, cookies, CORS and CSRF on the server for cross-origin use.
Never embed administrator tokens or shared secrets in public JavaScript.

The client exposes sessions, paginated history, sending, replay, snapshot subscription, cancellation, upload and human decisions.
A disconnected stream is not a completed task. Preserve run_id and event_offset for replay.
AbortSignal closes transport only; cancel stops a backend run.
Display confirmation/question events and submit explicit user decisions. Never automatically approve them.
Native snapshots replace the prior projection; a terminal snapshot can close normally without a DONE marker.
Unknown event types remain available to consumers.
The standalone example renders plain text and human interaction gates. It does not reproduce every rich product/domain renderer.
See packages/chat-sdk/README.md for the complete public contract.

## Docker and desktop builds

The standard frontend Dockerfile builds the SDK. Compose passes HUGAGENT_CHAT_CUSTOMIZATION as a build argument.
The source example uses ../../examples/customer-chat/src/customization.tsx.
Customer code, dependencies and CSS must enter the build context. External repositories use their own trusted install/COPY stages.
Upgrade backend and customer frontend images independently and test the chosen version pair.
Native desktop clients bundle the frontend and still require their platform-specific rebuild when customer UI changes.

## Verification

Use the WSL project root and Node 20+.
Run npm ci and npm test in packages/chat-sdk, then npm ci, npm run build and node scripts/test-chat-customization-contract.mjs in src/frontend.
The isolated chat-customization-environment.py script uses disposable containers, a temporary database and a deterministic model.
It does not read the real .env or business database.
Build the standalone example first. Start the environment, run test-chat-customization.mjs default, then test a prepared customer build with custom.
Stop the environment with down after testing. The internal implementation record contains exact commands and results.

## Independent customer developer kit

The SDK, customer source, host contract preview, login callback and API proxy can be exported without the main project source. Run node scripts/export-customer-devkit.mjs from the project root. Follow document/zh-CN/development/customer-devkit.md for installation and acceptance.
