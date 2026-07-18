# Sandbox Execution System

> Last updated: 2026-06-11

The sandbox is the isolated environment where HugAgentOS's agents execute code: every `bash` call the model makes in a conversation, every [skill](agent-skills.md) script run, every generated deliverable happens inside the sandbox rather than the backend process. A single **provider protocol** abstracts three interchangeable execution backends — from the single-host lightweight script_runner, through OpenSandbox with persistent sessions and snapshot recovery, to a remote MicroVM fleet (Cube) — with the tool layer above completely agnostic to the choice.

Edition split (see [editions](../editions/overview.md)): the **lightweight sandbox (script_runner) plus the sandbox tool / offload infrastructure are Community CE**; the **persistent sandboxes (opensandbox / cube — session retention, environment reuse, snapshot recovery) are Enterprise EE** — the CE derivation strips those two provider files and the factory transparently falls back to the lightweight implementation.

## The provider protocol (core/sandbox/protocol.py)

Every provider implements the same `SandboxProvider` Protocol, whose field contract aligns one-to-one with the script-runner sidecar's HTTP interface:

| Method | Responsibility |
|---|---|
| `execute(req: ExecuteRequest) -> ExecuteResult` | Run a script/command; returns stdout/stderr/exit_code/duration/output files |
| `stage_files(user_id, files)` | Stage input files into the user's myspace cache; returns sandbox-referenceable absolute paths |
| `put_file(session_id, path, content)` | Write bytes to a sandbox path (parent dirs auto-created) |
| `get_file(session_id, path)` | Read file bytes from the sandbox |
| `current_sandbox_id(session_id)` | Pure query of the currently bound sandbox identity (detects rebuilds) |
| `health()` | Health probe |
| `admin_*` family | Read-only views for the security console (capability declaration / instance listing / detail / pool stats); unsupported abilities raise `SandboxAdminNotSupported` and the UI greys them out |

Two key optional fields on `ExecuteRequest`:

- **`session_id`**: persistent providers use it to bind multiple `execute` calls to the same underlying sandbox instance (variables, pip packages and `/workspace` files persist across calls); ephemeral providers ignore it;
- **`user_id`**: enables myspace file visibility (bind-mount or seeding) — see Plan F below.

## The three provider implementations

| Provider | File | Form | When to use | Edition |
|---|---|---|---|---|
| `script_runner` | `script_runner_provider.py` | Wraps HTTP calls to the `hugagent-script-runner` sidecar container (setrlimit subprocesses inside), stateless | Single-host deployments, one-shot code execution; the default | CE |
| `opensandbox` | `opensandbox_provider.py` + `_opensandbox_*.py` | Alibaba OpenSandbox (Docker containers + persistent Jupyter kernels): per-chat persistent sessions, warm pools, snapshots | Multi-turn iterative analysis, heavy skill workflows | **EE** |
| `cube` | `cube_provider.py` | Tencent CubeSandbox (E2B-compatible MicroVMs) on a **remote node** — the backend reaches it over the network via the `e2b_code_interpreter` SDK, no local sidecar | Deployments where the backend host is resource-constrained, stronger (MicroVM-grade) isolation is required, or sandbox compute must scale independently | **EE** |

Switching is controlled by the `SANDBOX_PROVIDER` environment variable (`core/sandbox/factory.py`, a singleton factory). In a CE tree where the `opensandbox` / `cube` modules don't exist, the factory logs a warning and **falls back to script_runner automatically** — the same configuration still boots.

Cube's design trade-offs (the price of being remote): every language goes through "write a script file + `commands.run`" without Jupyter; **no host bind mounts** (myspace files are materialized via `put_file` by the tool layer, and skill files matching `/workspace/skills` are pushed at runtime, governed by `CUBE_SKILL_PREPUSH*`); no snapshot system; `session_id` still binds a persistent MicroVM (create on first use, connect-reuse afterwards).

## The three agent-side tools

`core/llm/tools/sandbox_tool.py` registers three tools with the agent (their `register_*` functions are invoked by `agent_factory` in Phase 3.5):

| Tool | Purpose |
|---|---|
| `bash(command, timeout)` | Run an arbitrary shell command in the sandbox; working dir `/workspace`, files persist within a session; hard cap 120 s; an upper-case `Bash` alias is also registered (some models emit Title-cased tool names by training convention) |
| `sandbox_put_artifact(artifact_id, dest_path)` | Copy a platform artifact's bytes (user uploads, chart-tool outputs, …) into a sandbox path — uploads are never auto-visible in the sandbox |
| `sandbox_get_artifact(src_path)` | Register a sandbox file as a downloadable artifact — bash outputs never auto-appear in the attachment area |

The sandbox session identity is resolved by `resolve_sandbox_session(sandbox_session_id, chat_id)`: main chat / plan execution → `chat_id` (per-chat persistent kernel); batch items / sub-agents → `""` (ephemeral).

**MySpace write-back loop**: when a `bash` command succeeds and its string mentions `myspace`, `_sync_myspace_changes` lists files modified in the last 10 minutes under the sandbox's `/workspace/myspace/{uid}`, md5-diffs them against the backend mirror cache, routes each changed file through the user-confirmation gate (`MYSPACE_WRITE_CONFIRM`; non-interactive contexts are denied outright), then writes approved changes back to "My Space" **in place under the same file_id** — download/preview links stay valid. This closes the gap where the model edits a docx with python-docx in the sandbox while the user's copy never changes.

## OpenSandbox session lifecycle (EE)

```
            ┌── warm pool SandboxPool (_pool.py, two buckets) ────────────┐
            │ jupyter bucket: min_idle=2  persistent sessions (Jupyter, ~10s)│
            │ light bucket:   min_idle=2  ephemeral runs (execd only, ~3s)   │
            └──────────────┬──────────────────────────────────────────────┘
   first bash              │ acquire
chat_id ──▶ _get_or_create_session ──▶ _Session (sandbox + CodeInterpreter + language ctxs)
                │                         │  reused on later calls; fire-and-forget renew
                │ idle > 600s (reaper)     │  repeated renew failures → stale → rebuilt next acquire
                ▼                         ▼
        returned to the user idle pool (Q2 warm reuse, ~7s reconnect)
                │ idle > 1500s (snapshot worker)
                ▼
        park: take_snapshot → wait Ready → upsert DB → kill the container
                │ user comes back
                ▼
        restore: Sandbox.create(snapshot_id=…) → full filesystem recovery
                                                  (kernel cold-boots, invisible to the user)
```

Key points (`_opensandbox_session.py` / `_opensandbox_internals.py`):

- **Per-chat heavy sandboxes**: one Jupyter-equipped container per conversation; variables, pip packages and `/workspace` files persist across bash calls;
- **TTL & renewal**: server-side sandbox TTL defaults to 1800 s (`OPENSANDBOX_DEFAULT_TIMEOUT_S`); every session activity triggers a rate-limited (60 s) background renew that never blocks the request path; renew failures distinguish lifecycle signals (immediate stale mark) from transient network errors (escalated only after 3 consecutive failures);
- **Two-layer warm pools**: the generic two-bucket pool is pre-warmed at process start; with Plan F enabled, user-bound traffic goes through a per-user `_JupyterUserPool` instead (a sandbox carrying one user's myspace volume must never be handed to another user), and the idle reaper (600 s, `OPENSANDBOX_IDLE_REAP_S`) returns idle sessions' sandboxes — kernels scrubbed — to the user idle pool for reuse rather than destroying them.

## Snapshot persistence (EE)

Full design in [sandbox-snapshot-design.md](../../sandbox-snapshot-design.md). Goal: stop idle sessions from squatting on Docker resources while letting users resume **with their filesystem intact**.

- **Park**: a background worker scans every 60 s; sessions idle beyond `OPENSANDBOX_IDLE_SNAPSHOT_THRESHOLD_S` (default 1500 s) get a snapshot (accept→Ready measured at ~60 s, a docker commit), then the `chat_sandbox_snapshots` row is upserted (chat_id is the primary key) and the container destroyed; at most 3 concurrent parks per round to protect the docker daemon;
- **Restore**: `_create_session_for` checks the Q2 user idle pool first (~7 s warm path), then the DB snapshot (~15–20 s restore, still faster than a fresh create), and only then creates fresh. When booting from a snapshot the volumes **must be re-declared** — docker commit does not preserve mount configuration, otherwise bind mounts like `/workspace/skills/` are lost;
- **Single-use**: once a snapshot has been consumed as a boot image it is marked for 1-hour short retention (an immediate DELETE would 409 because the new container still references the image layer);
- **GC**: an hourly sweep deletes expired snapshots (DB row + remote; default retention `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS=7`), retrying delete conflicts on the next round;
- master switch `OPENSANDBOX_SNAPSHOT_ENABLED` (default true); off reverts to the old "idle = gone" behaviour.

## MySpace bind mount (Plan F, EE)

With `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED=true` (the default), Jupyter sandbox creation bind-mounts the host's `$HOST_STORAGE_PATH/myspace_cache/{uid}/` directly into the sandbox at `/workspace/myspace/{uid}/` (`_opensandbox_internals.py::_make_myspace_volume`):

- the backend container's `/app/storage/myspace_cache/{uid}/` and the in-sandbox path point at the **same host inode** — "My Space" files are visible the moment the sandbox starts, eliminating the old full-directory HTTP PUT sync;
- prerequisites: `HOST_STORAGE_PATH` must exactly match the host path docker-compose uses for the backend storage volume, and the OpenSandbox server's `allowed_host_paths` must include that prefix;
- with the flag off or `user_id` missing, the old HTTP PUT sync path (`_sync_inputs_to_sandbox`) takes over automatically;
- companion isolation rule: a sandbox carrying a user's volume may only return to **that user's** idle pool, never the generic one.

## Read-only skills mount

All skill files are exposed inside the sandbox through a **single read-only bind mount** at `/workspace/skills/<id>` (`_make_skills_volume`). The mount source is the unified skills directory `$HOST_STORAGE_PATH/sandbox_skills` (built-ins synced in at startup, DB skills materialized on demand — see the [skill system](agent-skills.md)). Read-only guarantees skills cannot be tampered with from inside the sandbox; the directory bind is live, so newly imported skills are visible immediately. Without `HOST_STORAGE_PATH` it falls back to mounting only the built-in source tree, with a warning.

## Offloading oversized tool results

`core/llm/offloader.py::SandboxOffloader` implements AgentScope 2.0's `Offloader` protocol: when context compression or tool-result truncation kicks in, the overflow is no longer silently dropped — it is written via `put_file` into the hidden sandbox directory **`/workspace/.offload/`** (`tool_<call_id>.txt` / `context_<hex>.txt`), and the path is woven into the model-facing system-reminder, so the model can read the full content back on demand with `Read` or `bash(cat/grep …)`. The protocol requires these methods to **never raise** (write failures return a degradation note), and the offloader is only attached when sandbox tools are enabled.

## Administrator sandbox management

- **Read-only monitoring (security console)**: `api/routes/v1/config_security.py` exposes `/v1/config/security/sandbox/*` — overview, instance list, per-instance detail, snapshot list, rebuild history and effective configuration; everything goes through the providers' `admin_*` interfaces, with the UI trimmed by each provider's `admin_capabilities()` declaration (script_runner can't enumerate instances, so those columns are greyed out).
- **Dependency rebuild (Enterprise EE)**: `api/routes/v1/admin_sandbox.py` (`/v1/admin/sandbox/*`) aggregates the pip/apt dependencies declared by all skills (`core/services/skill_deps_aggregator.py`) and lets an admin trigger a sandbox image rebuild with one click: the `script-runner` / `opensandbox` targets run a local `docker compose build`, while the `cube` target rebuilds the template on the remote node over SSH and hot-swaps it (`core/services/sandbox_rebuild_service.py` + `cube_template_builder`), with per-run status and logs. New skill dependencies get baked into the images without hand-editing Dockerfiles.

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `SANDBOX_PROVIDER` | `script_runner` | Provider selection: `script_runner` / `opensandbox` / `cube` |
| `SANDBOX_RUNNER_URL` | `http://hugagent-script-runner:8900` | script_runner sidecar address |
| `OPENSANDBOX_DOMAIN` / `OPENSANDBOX_API_KEY` / `OPENSANDBOX_IMAGE` | — | OpenSandbox server & image |
| `OPENSANDBOX_DEFAULT_TIMEOUT_S` | 1800 | Server-side sandbox TTL |
| `OPENSANDBOX_POOL_{JUPYTER,LIGHT}_{MIN,MAX}_IDLE` / `OPENSANDBOX_POOL_MAX_TOTAL` | 2/3, 2/5, 20 | Warm-pool watermarks |
| `OPENSANDBOX_IDLE_REAP_S` | 600 | Idle-session reap (return-to-idle-pool) threshold |
| `OPENSANDBOX_SNAPSHOT_ENABLED` | true | Snapshot system master switch |
| `OPENSANDBOX_IDLE_SNAPSHOT_THRESHOLD_S` | 1500 | Idle time before parking |
| `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS` | 7 | Snapshot retention |
| `OPENSANDBOX_SNAPSHOT_WAIT_TIMEOUT_S` | 120 | Max wait for snapshot Ready |
| `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED` | true | Plan F myspace direct-mount switch |
| `HOST_STORAGE_PATH` | — | Real host path of the storage volume (bind-mount source) |
| `SANDBOX_SKILLS_DIR` | `$STORAGE_PATH/sandbox_skills` | Unified skills directory override |
| `MYSPACE_WRITE_CONFIRM` | true | Hard user-confirmation gate for /myspace writes |
| `CUBE_API_URL` / `CUBE_API_KEY` / `CUBE_TEMPLATE` / `CUBE_API_SANDBOX_DOMAIN` | — | Cube node connection |
| `CUBE_IDLE_REAP_S` / `CUBE_POOL_MIN_IDLE` / `CUBE_OWNER_TAG` | 600 / 2 / — | Cube reaping, pre-warm, ownership tag for shared nodes |
| `CUBE_SKILL_PREPUSH*` | true / 20 MB / 3 | Skill pre-push switch / size cap / concurrency |
| `CUBE_NODE_SSH_*` / `CUBE_BUILD_*` | — | Remote-node SSH and build parameters for the admin dependency rebuild |

Full list in the [environment variable reference](../deployment/environment-variables.md).

## Source map

| Path | Description |
|---|---|
| `src/backend/core/sandbox/protocol.py` | Provider protocol & data contracts |
| `src/backend/core/sandbox/factory.py` | Provider singleton factory + CE fallback |
| `src/backend/core/sandbox/script_runner_provider.py` | Lightweight sandbox (CE) |
| `src/backend/core/sandbox/opensandbox_provider.py` | OpenSandbox provider body (EE) |
| `src/backend/core/sandbox/_opensandbox_session.py` | Sessions / snapshots / park-restore workers (EE) |
| `src/backend/core/sandbox/_opensandbox_exec.py` | Execution path + idle reaper (EE) |
| `src/backend/core/sandbox/_opensandbox_internals.py` | Volume builders, metadata, user pool (EE) |
| `src/backend/core/sandbox/_pool.py` | Two-bucket warm pool |
| `src/backend/core/sandbox/cube_provider.py` | Cube remote-MicroVM provider (EE) |
| `src/backend/core/llm/tools/sandbox_tool.py` | bash / sandbox_put_artifact / sandbox_get_artifact |
| `src/backend/core/llm/offloader.py` | Overflow offloading to /workspace/.offload |
| `src/backend/api/routes/v1/admin_sandbox.py` | Dependency-rebuild admin API (EE) |
| `src/backend/api/routes/v1/config_security.py` | Security-console read-only sandbox views |
| `src/backend/core/services/sandbox_rebuild_service.py` | Image/template rebuild orchestration (EE) |
| `docker/Dockerfile.script-runner` / `docker/Dockerfile.opensandbox` / `docker/Dockerfile.cube-sandbox` | The three sandbox images |

Related docs: [Agent skills](agent-skills.md) · [MCP tool system](mcp-tools.md) · [Projects & My Space](projects-myspace.md) · [Editions & licensing](../editions/overview.md)
