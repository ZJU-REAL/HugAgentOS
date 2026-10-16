# Workbench administration

[中文](../../zh-CN/development/workbench-administration.md) · [preview](workbench-preview.md)

Use the authenticated Config application for organization configuration (EE).
Do not change existing managed SQL/MCP business applications to create a Workbench entry.
Workbench endpoints use /v1/workbench. Existing business endpoints keep their authorization.

## External website

Create an external application declaration with a stable ID and a neutral title.
The workspace example includes external-application.json.

```json
{
  "schemaVersion": "1.0.0",
  "id": "weather",
  "title": "Weather",
  "target": {"kind": "external", "mode": "browser", "url": "https://example.org/weather"}
}
```

Validate the declaration before submitting it through Config.
Ordinary websites open in the browser and retain the host page.
Embedding requires an explicit trusted-origin configuration and the target's support.
If CSP, frame headers or cookies prevent embedding, provide a browser-open action.
Do not strip the target's headers or deploy an arbitrary proxy.

## Authentication and permissions

Choose independent login, shared identity provider or a target-limited exchange.
Independent login uses the target's own session.
Shared identity means both systems authenticate separately with the same provider.
Never forward host cookies or a general SSO token.
An exchange uses a short-lived, single-use ticket bound to user, application, audience and exact callback.
The target server authenticates when exchanging the ticket.
Keep exchange secrets and upstream data-source credentials in protected server configuration.
Do not put tickets or credentials into public manifests, screenshots, logs or URLs.
Host logout revokes host resources. Global target logout requires explicit target support.

Configure catalog visibility separately from actual capability approval.
Pinning or hiding an application is a layout action.
Existing allowed_apps remains an application visibility policy.
Launch, data and file access still require actual authorization and ownership.
Use the exact catalog applicationKey in policy selectors.
An external ID does not grant a same-named plugin authority.

## Plugin acceptance

Validate and inspect the package before installation.
Review requested module grants, data-source destinations and permission changes.
Open the installed page from App Center. Unopened applications must create no iframe or process.
Test allowed and denied actions with isolated accounts and temporary files.
Disable the installation while it is open. Old channels and access must stop.
Upgrades that request new permissions require approval.
Retain the original package and protected data snapshot for recovery.

Read [errors](workbench-errors.md) and [migration](workbench-migration.md).
