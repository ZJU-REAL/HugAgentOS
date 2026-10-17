# Customer chat example

Two independent consumers are included: a scoped host UI package and a standalone browser application.
Both import only the published SDK. The current file dependency is a monorepo convenience; replace it with a pinned SDK tarball/version when copying this directory to a customer repository.

```sh
npm install /path/to/hugagent-chat-sdk-1.0.0.tgz
npm run build
npm pack
```

Install the resulting customer package in src/frontend and build the platform with HUGAGENT_CHAT_CUSTOMIZATION=hugagent-customer-chat-example.
For this repository's source example:

```sh
cd src/frontend
HUGAGENT_CHAT_CUSTOMIZATION=../../examples/customer-chat/src/customization.tsx npm run build
```

Unset that variable to build the official default interface.
A customer package must use the same SDK major and React major as the host. The host deduplicates React and the SDK.

The example wraps default welcome/composer/message components. Customer source, CSS and lockfile live independently.
For a replacement component, use useChatRuntime for draft, messages, attachments and actions.
Preserve children for rich platform controls that the replacement does not implement.

`npm run standalone` produces standalone-dist, served at /customer/ by the acceptance environment.
Set VITE_CUSTOMER_API_BASE (default /api) and VITE_CUSTOMER_LOGIN_URL (default /login?redirect=/session.html) when building for your platform.
The default login link opens the platform's normal login page. It does not depend on mock SSO.
Use a same-origin API gateway and an authenticated platform session. Do not embed shared API keys in a public build.
The standalone example renders plain-text replies, paginated history, attachment upload, cancellation, replay, and tool/question gates.
It intentionally does not implement the entire platform's rich artifact, citation, plan and ontology UI; use default host children for those features.

Customer UI images and bundles must be deployed separately from backend images. Pin both versions and test the desired combination before upgrade.
The Docker example's customer source is inside the build context; external customer repositories require their own trusted build-stage COPY/install steps.

For the complete no-source developer kit, run node scripts/export-customer-devkit.mjs from the repository root. See document/zh-CN/development/customer-devkit.md. The kit provides npm ci, npm run dev, offline slot preview, backend login, fixed API proxy, API debug and npm run test:package. Component builds bundle imported helpers, CSS and assets.
