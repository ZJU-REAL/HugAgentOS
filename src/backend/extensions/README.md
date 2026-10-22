# Bundled extensions

This directory owns extensions shipped with the backend. `paths.py` defines
absolute resource roots; runtime code must use these roots instead of walking
up from its own module with `parents[n]`.

| Directory | Contents |
| --- | --- |
| `skills/default/` | Always available built-in skills |
| `skills/marketplace/` | Installable skill packages |
| `agents/default/<role>/` | Platform agent definitions (`agent.json`) and prompt seeds (`system.md`) |
| `agents/marketplace/` | Installable preset agent packages |
| `plugins/default/`, `plugins/marketplace/` | Plugin manifests and bundled components |
| `mcp_servers/` | MCP server implementations, registry, ports and process launcher |

Platform roles keep the IDs `builtin.explorer`, `builtin.worker` and
`builtin.reviewer`. Their manifests define identity, context sharing, permission
policy and iteration limits. Core runtime code applies the policy against the
parent's actual grants. Active database prompt parts (`subagents` kind) take
precedence over the shipped Markdown. The files remain the seed and fallback.

Lifecycle code stays in `core/capabilities/skills/`, `core/capabilities/plugins/` and
`core/capabilities/`. Agent execution stays in `core/llm/` and `orchestration/`.
User installations, credentials and mutable state belong to the configured
runtime stores, never this directory.

Run MCP processes from the backend import root:

```bash
PYTHONPATH=src/backend python -m extensions.mcp_servers._launcher
```

Resource moves must update Python packaging, Docker COPY and volume paths,
CE exclusions/overlays/asset allowlists, documentation and consumer tests
together. Old Python import paths are removed; public IDs, ports and API
contracts remain stable.
