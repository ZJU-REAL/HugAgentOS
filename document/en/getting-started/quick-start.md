# Quick start

> Last updated: July 19, 2026 ｜
> [简体中文](../../zh-CN/getting-started/quick-start.md)

Install HugAgentOS on one machine with one command. This profile runs the web
application, API, SQLite database, in-process state, MCP tools, and local
subprocess sandbox without Docker, PostgreSQL, or Redis.

## Requirements

Install these tools before you start. You also need access to an LLM API or a
local model with an OpenAI-compatible endpoint.

| Item | Requirement |
|---|---|
| Operating system | Linux, macOS, or Windows through WSL2 |
| Python | 3.10 or later |
| Node.js | 20 or later, with npm |
| Git and curl | Available on `PATH` |

## Install

Run the public installer from any directory:

```bash
curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
```

The installer downloads the latest Community Edition to
`~/.hugagent/source`, creates an isolated environment at
`~/.hugagent/venv`, installs the dependencies, and builds the web application.
It then starts the interactive setup wizard.

## Complete the first-run setup

Use the terminal wizard to create an administrator account and configure your
chat model. Embedding, reranker, file-parser, and internet-search services are
optional and can be added later.

After setup, HugAgentOS starts automatically and opens
[http://127.0.0.1:3001](http://127.0.0.1:3001).

## Start HugAgentOS again

Run the installed command whenever you want to start the application again:

```bash
~/.hugagent/venv/bin/hugagent
```

You can add the command to your shell path:

```bash
export PATH="$HOME/.hugagent/venv/bin:$PATH"
```

## Choose a production deployment

The one-command profile is for personal trials and development. It uses one
process, SQLite, in-process sessions, and a host subprocess sandbox. For teams,
high availability, or production isolation, use the
[Docker Compose deployment guide](../deployment/docker-compose.md).

## Next steps

Continue with these guides after your first successful login:

- Read the [complete no-Docker installation guide](../deployment/quick-install.md)
  for installer options, capability boundaries, and troubleshooting.
- Configure [model providers](../modules/model-providers.md).
- Explore [MCP tools](../modules/mcp-tools.md),
  [Agent Skills](../modules/agent-skills.md), and
  [private knowledge bases](../modules/knowledge-base.md).
