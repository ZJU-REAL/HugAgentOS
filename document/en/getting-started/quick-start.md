# Quick start

> Last updated: October 23, 2026 ｜
> [简体中文](../../zh-CN/getting-started/quick-start.md)

There are three ways to get started. Choose HugAgentOS Cloud when agents should keep running in the
cloud around the clock, the HugAgentOS Local desktop client to work offline on your own computer, or
the one-command installer for development and quick trials. Every option needs access to an
OpenAI-compatible API or a local model.

## Choose a deployment method

| Method | Best for | Runtime |
|---|---|---|
| HugAgentOS Cloud (Docker Compose) | 24/7 operation, Personal Agent, IM channels, access from any device | PostgreSQL, Redis, OpenSandbox, persistent volumes |
| HugAgentOS Local (desktop client) | Offline use, sensitive data, local projects | Bundled private runtime, local SQLite and files |
| One-command installer | Trials and development | SQLite, in-process state, local subprocess sandbox |

**The initial account and password are both `admin` and must be changed on first sign-in; the
Community Edition has no self-registration.**

## Option 1: HugAgentOS Cloud (Docker Compose)

Install Git, Docker Engine or Docker Desktop, and Docker Compose v2, then run:

```bash
git clone https://github.com/ZJU-REAL/HugAgentOS.git
cd HugAgentOS
cp .env.example .env
mkdir -p data/storage
docker compose build backend
docker compose up -d --build
```

Open [http://localhost:3002](http://localhost:3002). Sign in with `admin` /
`admin`, change the password, then open **Settings → System Administration →
Model Services** to connect a model. Use `docker compose ps` to inspect the
services and `docker compose down` to stop them without deleting data.

Read the [Docker Compose deployment guide](../deployment/docker-compose.md) for
profiles, persistence, production configuration, and rebuild workflows.

### Enable Personal Agent (optional)

Personal Agent is off by default. It uses a dedicated OpenSandbox controller to keep a permanent
cloud computer for each user.

1. Build the computer image:

   ```bash
   docker build -f docker/personal-agent/Dockerfile.personal-agent -t hugagent-personal-agent:v5 .
   ```

2. Set these values in `.env`:

   | Variable | Value |
   |---|---|
   | `PERSONAL_AGENT_ENABLED` | `true` |
   | `HOST_STORAGE_PATH` | An absolute path on the host |
   | `PERSONAL_AGENT_OPENSANDBOX_API_KEY` | A separate random secret of at least 32 characters, for example the output of `openssl rand -hex 32` |

3. Start with the additional Compose file:

   ```bash
   docker compose -f docker-compose.yml -f docker/personal-agent/compose.personal-agent.yml --profile personal-agent up -d
   ```

Then click **Personal Agent** under **New chat**. See [Personal Agent](../modules/personal-agent.md)
for database migrations, image upgrades and channel binding. Never commit the secret to Git.

## Option 2: HugAgentOS Local (desktop client)

1. Download the desktop installer for your platform (Windows x86_64, macOS Apple Silicon / Intel,
   Linux x86_64) from [Releases](https://github.com/ZJU-REAL/HugAgentOS/releases).
2. Choose a mode on first launch:
   - **Local only**: the service, data and execution stay on this computer and can run fully
     offline;
   - **Cloud only**: connect to a deployed HugAgentOS Cloud;
   - **Local + cloud**: use your cloud account; cloud projects stay in the cloud and local
     projects run on this machine, which also lets Personal Agent connect to this computer.
3. In local mode, the client verifies and unpacks its bundled runtime, usually within tens of
   seconds, with no Python, Docker or database to install.

See [Desktop local mode](../deployment/desktop-local-mode.md) for installation, upgrade rollback
and data directories, and [Local project workspace](../deployment/local-project-workspace.md) for
how execution works in a bound local project.

## Option 3: one-command installer

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
~/.hugagent/venv/bin/hugagent-server
```

You can add the command to your shell path:

```bash
export PATH="$HOME/.hugagent/venv/bin:$PATH"
```

The one-command installer has no cross-process lease takeover and is not meant for 24/7 operation.

## Next steps

Continue with these guides after your first successful login:

- Read the [introduction](introduction.md) for the durable agent, runtime forms and feature overview.
- Configure [model providers](../modules/model-providers.md).
- Try [Personal Agent](../modules/personal-agent.md) and the
  [autonomous loop](../modules/autonomous-loop.md).
- Explore [MCP tools](../modules/mcp-tools.md),
  [Agent Skills](../modules/agent-skills.md), and
  [private knowledge bases](../modules/knowledge-base.md).
- Build a governed workflow with the
  [domain ontology quickstart](domain-ontology-quickstart.md).
- Read the [complete no-Docker installation guide](../deployment/quick-install.md)
  for installer options, capability boundaries, and troubleshooting.
