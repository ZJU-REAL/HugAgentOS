# Quick start

> Last updated: July 21, 2026 ｜
> [简体中文](../../zh-CN/getting-started/quick-start.md)

Choose the one-command installer for a personal trial or Docker Compose for a
server-oriented, service-isolated deployment. Both methods require access to
an OpenAI-compatible API or local model.

## Choose a deployment method

The two deployment methods use different runtime and persistence models.

| Method | Best for | Runtime |
|---|---|---|
| One-command installer | Personal trials and development | SQLite, in-process state, local subprocess sandbox |
| Docker Compose | Long-running servers and service isolation | PostgreSQL, Redis, container sandbox, persistent volumes |

## Option 1: one-command installer

Use this method on Linux, macOS, or Windows through WSL2. Install the following
tools before you start.

| Item | Requirement |
|---|---|
| Operating system | Linux, macOS, or Windows through WSL2 |
| Python | 3.11 or later |
| Node.js | 20 or later, with npm |
| Git and curl | Available on `PATH` |
| Rust and Cargo | Required only on Linux without a compatible prebuilt `ripgrep` wheel, including glibc versions earlier than 2.39 |

### Install

Run the public installer from any directory:

```bash
curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
```

The installer downloads the latest Community Edition to
`~/.hugagent/source`, creates an isolated environment at
`~/.hugagent/venv`, installs the dependencies, and builds the web application.
It then starts the interactive setup wizard.

### Complete the first-run setup

Use the terminal wizard to create an administrator account and configure your
chat model. Embedding, reranker, file-parser, and internet-search services are
optional and can be added later.

After setup, HugAgentOS starts automatically and opens
[http://127.0.0.1:3001](http://127.0.0.1:3001).

### Start HugAgentOS again

Run the installed command whenever you want to start the application again:

```bash
~/.hugagent/venv/bin/hugagent
```

You can add the command to your shell path:

```bash
export PATH="$HOME/.hugagent/venv/bin:$PATH"
```

## Option 2: Docker Compose

Use this method when you need durable service volumes and container isolation.
Install Git, Docker Engine or Docker Desktop, and Docker Compose v2, then run:

```bash
git clone https://github.com/ZJU-REAL/HugAgentOS.git
cd HugAgentOS
cp .env.example .env
mkdir -p data/storage
docker compose up -d --build
```

Open [http://localhost:3002](http://localhost:3002). Sign in with `admin` /
`admin`, change the password, then open **Settings → System Administration →
Model Services** to connect a model. Use `docker compose ps` to inspect the
services and `docker compose down` to stop them without deleting data.

Read the [Docker Compose deployment guide](../deployment/docker-compose.md) for
profiles, persistence, production configuration, and rebuild workflows.

## Next steps

Continue with these guides after your first successful login:

- Read the [complete no-Docker installation guide](../deployment/quick-install.md)
  for installer options, capability boundaries, and troubleshooting.
- Configure [model providers](../modules/model-providers.md).
- Build a governed workflow with the
  [domain ontology quickstart](domain-ontology-quickstart.md).
- Explore [MCP tools](../modules/mcp-tools.md),
  [Agent Skills](../modules/agent-skills.md), and
  [private knowledge bases](../modules/knowledge-base.md).
