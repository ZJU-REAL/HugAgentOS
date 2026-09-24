# Sandbox Execution System

> Last updated: 2026-09-24

The sandbox is the isolated environment where HugAgentOS's agents execute code: every `bash` call the model makes in a conversation, every [skill](agent-skills.md) script run, every generated deliverable happens inside the sandbox rather than the backend process. A single **provider protocol** abstracts three interchangeable execution backends — from the single-host lightweight script_runner, through OpenSandbox with persistent sessions and snapshot recovery, to a remote MicroVM fleet (Cube) — with the tool layer above completely agnostic to the choice.

Edition split (see [editions](../editions/overview.md)): the **lightweight sandbox (script_runner) plus the sandbox tool / offload infrastructure are Community CE**; the **persistent sandboxes (opensandbox / cube — session retention, environment reuse, snapshot recovery) are Enterprise EE** — the CE derivation strips those two provider files and the factory transparently falls back to the lightweight implementation.

All providers follow one boundary rule: **`session_id` (`chat_id` in a conversation) is the sole isolation key**. Different conversations use different sandboxes; the main agent, built-in subagents, user-defined subagents, and batch items in one conversation share the same `/workspace`. Model context, tool permissions, and execution threads may remain independent, but no second file sandbox is created inside a conversation.

## The provider protocol (core/sandbox/protocol.py)

Every provider implements the same `SandboxProvider` Protocol, with an internal completion API wrapping the managed process protocol; script-runner transports process operations over the sidecar HTTP interface:

| Method | Responsibility |
|---|---|
| `run_to_completion(req: ProcessRequest)` | Internal business API: await completion and return final ProcessResult |
| `start_process(req: ProcessRequest, yield_time_ms=60000)` | Start a process; return incremental output, status, process ID or exit code |
| `write_stdin(session_id, chars="", yield_time_ms=60000)` | Wait for incremental output or interrupt the process |
| `stage_files(user_id, files)` | Stage input files into the user's myspace cache; returns sandbox-referenceable absolute paths |
| `put_file(session_id, path, content)` | Write bytes to a sandbox path (parent dirs auto-created) |
| `get_file(session_id, path)` | Read file bytes from the sandbox |
| `get_file_to_path(session_id, path, destination, max_bytes)` | Stream a sandbox file into a backend temporary file while enforcing the size limit during transfer |
| `current_sandbox_id(session_id)` | Pure query of the currently bound sandbox identity (detects rebuilds) |
| `health()` | Health probe |
| `admin_*` family | Read-only views for the security console (capability declaration / instance listing / detail / pool stats); unsupported abilities raise `SandboxAdminNotSupported` and the UI greys them out |

Internal business code calls await provider.run_to_completion(request) to receive a final ProcessResult (stdout, stderr, exit_code, execution_time_ms), without managing process IDs or invoking model tools. All three providers share process management and result collection; waiting stays inside the provider, with one launch and no automatic retry. The model tool surface remains bash / write_stdin. Commands have no default execution deadline; callers can explicitly set timeout. Individual waits, network request timeouts, and cloud sandbox lifetimes are separate limits.

Two key fields on `ProcessRequest`:

- **`session_id`**: every provider uses it to route the conversation workspace; calls with the same ID retain `/workspace` files, while different IDs cannot see each other. OpenSandbox and Cube additionally reuse the underlying container, MicroVM, or kernel;
- **`user_id`**: enables myspace file visibility (bind-mount or seeding) — see Plan F below.

## The three provider implementations

| Provider | File | Form | When to use | Edition |
|---|---|---|---|---|
| `script_runner` | `script_runner_provider.py` | One sidecar with persistent per-`session_id` filesystem directories; each command still runs in a setrlimit subprocess | Single-host lightweight execution; the default | CE |
| `opensandbox` | `opensandbox_provider.py` + `_opensandbox_*.py` | Alibaba OpenSandbox (Docker containers + persistent Jupyter kernels): per-chat persistent sessions, warm pools, snapshots | Multi-turn iterative analysis, heavy skill workflows | **EE** |
| `cube` | `cube_provider.py` | Tencent CubeSandbox (E2B-compatible MicroVMs) on a **remote node** — the backend reaches it over the network via the `e2b_code_interpreter` SDK, no local sidecar | Deployments where the backend host is resource-constrained, stronger (MicroVM-grade) isolation is required, or sandbox compute must scale independently | **EE** |

Switching is controlled by the `SANDBOX_PROVIDER` environment variable (`core/sandbox/factory.py`, a singleton factory). In a CE tree where the `opensandbox` / `cube` modules don't exist, the factory logs a warning and **falls back to script_runner automatically** — the same configuration still boots.

Cube's design trade-offs (the price of being remote): every language goes through "write a script file + `commands.run`" without Jupyter; **no host bind mounts** (myspace files are materialized via `put_file` by the tool layer, and skill files matching `/workspace/skills` are pushed at runtime, governed by `CUBE_SKILL_PREPUSH*`); no snapshot system; `session_id` still binds a persistent MicroVM (create on first use, connect-reuse afterwards).

## Agent-side tools

`core/llm/tools/sandbox_tool.py` registers four tools with the agent (their `register_*` functions are invoked by `agent_factory` in Phase 3.5):

| Tool | Purpose |
|---|---|
| `bash(command, timeout=None, yield_time_ms=60000)` | Start a command and wait 60 seconds by default; unfinished commands return a process `session_id` and keep running. Omitted timeout sets no command deadline; retains the `Bash` alias |
| `write_stdin(session_id, chars="", yield_time_ms=60000)` | Wait for the same process and read incremental output; empty input waits, Ctrl+C (\u0003) requests interruption |
| `sandbox_put_artifact(artifact_id, dest_path)` | Copy a platform artifact's bytes (user uploads, chart-tool outputs, …) into a sandbox path — uploads are never auto-visible in the sandbox |
| `sandbox_get_artifact(src_path)` | Stream a sandbox file into a downloadable artifact; the default per-file limit is 100 MiB — bash outputs never auto-appear in the attachment area |


Execution deadlines and wait windows are separate: initial waits are 250–30000 ms; follow-up waits are capped at 300000 ms. A wait expiring never terminates the command. Explicit `timeout` remains an execution deadline and is no longer clamped to 120 seconds. All commands and internal Python/JavaScript scripts use the process API. The synchronous execution endpoint and default/maximum execution timeout settings have been removed. Process IDs are bound to the initiating user and conversation. This is non-PTY execution with closed stdin: only empty input and Ctrl+C are accepted. On headless Windows, interruption terminates the process tree.

The runtime retains at most 64 process records and only evicts completed records. Pending output is bounded; `output_omitted_chars` reports omissions. Local/script_runner logs remain in the conversation workspace at `.__process_*/stdout.log` and `stderr.log`; `output_files` returns their real readable paths. A stream exceeding 64 MiB stops the command and preserves the written log. OpenSandbox background logs merge stdout/stderr; Cube commands stop above 64 MiB total output. Active commands periodically touch their sandbox: OpenSandbox can renew its lease, while compatible Cube servers may not support extending their server-side TTL; `lifetime_note` reports that limitation. Handles are runtime state, with no guarantee of recovery across backend/runner restarts. Explicit session close and graceful service shutdown clean up managed commands. Team source changes synchronize when terminal output is collected; running does not mean saved.

The sandbox identity is resolved by `resolve_sandbox_session(sandbox_session_id, chat_id)`: a non-empty explicit ID wins, while a missing or empty value falls back to `chat_id`. The main agent, plan execution, batch items, and every subagent therefore use one conversation sandbox, and a child run never destroys it when it finishes.

**MySpace real-time registration**: a file written under `/myspace` **is** a file in the user's My Space, and both sides must show the same state at any moment. Registration is driven by **the filesystem itself** (`core/myspace/watcher.py`): any write or delete under the mirror directory `myspace_cache/{uid}/` is registered back into the artifact ledger, **regardless of who wrote it**.

The earlier design had each writing tool register its own change (write / edit / file delete-move-mkdir / a before-and-after directory snapshot around `bash`). That assumes changes can only arrive through those entry points, and they cannot: a `nohup`-ed background process writes after the command returns, sub-agents and batch runs write from another coroutine, skill CLIs write directly, and so do MCP servers. Every entry point left out shows up the same way — the file sits on the user's disk, is invisible and undeletable in the UI, and every new sandbox mounts that directory again, which is where "leftover intermediate files from the previous session" came from.

| When | Direction | What happens |
|---|---|---|
| A write or delete in the directory | Sandbox → My Space | Registered as an artifact, or the deleted file is soft-deleted from My Space |
| Before every `bash` | My Space → sandbox | UI uploads/edits land in the mirror directory (immediately visible under the bind mount); files deleted in the UI are removed from the mirror |

Registration splits by the nature of the file — it is **not** a blanket push:

- **New files** (no artifact record yet) → registered and shown directly, without interrupting the user. The bytes already sit on the user's disk, so blocking here would only mint invisible-but-undeletable files that the next session still mounts.
- **Edits to / deletions of the user's existing files** → routed through the `MYSPACE_WRITE_CONFIRM` gate. The file on disk has already changed, so the gate cannot block; what it does is **restore that version from object storage** when the user declines. Where nobody can answer (no live chat, sub-agents, batch runs, scheduled tasks) the change applies directly instead of being denied — denial cannot undo what already happened, it only leaves the two sides inconsistent. The gate also honours the user's approval preset, so "full access" no longer prompts per file.
- **Files the user deleted are never resurrected**: leftover mirror copies are never registered — the forward pass removes them instead. Only a same-named file written *after* the deletion counts as new content.

Other points:

- **Registration attaches no chat, and pins no workspace card**: MySpace is one per-user directory that every session's sandbox mounts, so a filesystem event carries a path and no chat identity — who wrote it is simply not knowable here. Registration therefore records only the owning user (`Artifact.chat_id` stays null), and the registry never pins: a card can only be pinned by the run that wrote the file (the model calling `pin_to_workspace`, or that run's own Write / delivery guard — all through the `core.llm.workspace` ContextVar), where both "which chat" and "which file" are certain. Picking "whichever run of this user happens to be live" instead used to file one chat's output under another chat and surface it in that chat's workspace.
- **The reference point is the artifact record, not the mirror cache**: with the bind mount on, the sandbox's `/workspace/myspace/{uid}` *is* the backend's `myspace_cache/{uid}`, so "sandbox file matches mirror cache" is trivially true and can no longer mean "already registered". That mismatch used to make write-back a permanent no-op.
- **"Newer on disk than in the ledger" is the only signal**: when the backend writes into the mirror itself (materialising a UI upload, restoring a declined change) it aligns the file's mtime to the registration time, so the watcher never mistakes the backend's own write for a fresh sandbox change — no separate "I wrote this" bookkeeping is needed.
- **Every worker watches; a claim deduplicates**: the confirmation bar lives in the process running that chat, so the watcher has to run in every worker (the cost is inotify watches scaling as workers × directories against `fs.inotify.max_user_watches`; raise it on deployments with very many folders — exceeding it drops events **silently**); one change is still handled once, via the claim in `core.infra.ephemeral`, and preferably by the worker hosting that user's chat (only it can raise the bar).
- **When the sandbox directory is not on this host** (`script_runner` / `cube`: the session workspace lives inside a container, or the whole sandbox is remote), `core/myspace/sandbox_sync.py` copies the sandbox's current state into the mirror directory after the command, and everything downstream is identical.

The backlog that accumulated before this shipped is a **one-off data migration**: run `scripts/reconcile_myspace_mirror.py` by hand once (`--dry-run` first; add `--prune-stale` to clear leftover copies). It is deliberately not a scan that runs on every startup — that would be wasted work, and it would paper over real gaps in the live path.

### Large artifact delivery

`sandbox_get_artifact` supports individual files up to 100 MiB (104,857,600
bytes) by default. `SANDBOX_ARTIFACT_MAX_BYTES` is the **single switch** for
sandbox file size, bounding all four paths: explicit `sandbox_get_artifact`
fetches, artifacts auto-collected after a `bash` run (both per-file and
per-batch total), artifacts pushed into the sandbox, and the `/myspace`
write-back sync after a `bash` run; the backend and the script-runner sidecar
read the same variable.
All three providers implement `get_file_to_path`: script_runner uses a raw HTTP
response stream, OpenSandbox uses `read_bytes_stream`, and Cube uses the E2B
`format="stream"` mode. The backend writes streamed chunks to a temporary file,
then copies that file into the local artifact directory or uploads it to OSS
through the file-based storage API. The transfer doesn't convert the complete
binary to Base64 or retain the complete file in backend memory.

Inline artifacts returned by sandbox `execute` keep their 10 MiB per-file and
20 MiB per-call safety limits because those files enter the tool JSON payload.
Use `sandbox_get_artifact` explicitly for larger deliverables. If a file exceeds
the configured limit before or during transfer, the tool returns the structured
`sandbox_artifact_too_large` error. Split PDFs by page; split other archive-like
formats or reduce their content before delivering each part.

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
- **TTL & renewal**: the server-side sandbox TTL is `SANDBOX_IDLE_TTL_S` (default 3600 s — the same number as the idle threshold); every session activity triggers a rate-limited (60 s) background renew that never blocks the request path; renew failures distinguish lifecycle signals (immediate stale mark) from transient network errors (escalated only after 3 consecutive failures);
- **Two-layer warm pools**: the generic two-bucket pool is pre-warmed at process start; with Plan F enabled, user-bound traffic goes through a per-user `_JupyterUserPool` instead (a sandbox carrying one user's myspace volume must never be handed to another user), and the idle reaper (`SANDBOX_IDLE_TTL_S`, default 3600 s) snapshots an idle session and then returns its sandbox — kernels scrubbed and `/workspace` wiped — to the user idle pool for reuse rather than destroying it. The wipe keeps only the `myspace` and `skills` mount points and rebuilds `scratch`: the container is reused across chats, so scripts and intermediate files left in `/workspace` would be read by the next session as its own context. If the wipe fails the sandbox is destroyed instead of reused.

## Snapshot persistence (EE)

Full design in [sandbox-snapshot-design.md](../../sandbox-snapshot-design.md). Goal: stop idle sessions from squatting on Docker resources while letting users resume **with their filesystem intact**.

- **Park**: a background worker scans every 60 s; sessions idle beyond `SANDBOX_IDLE_TTL_S` (default 3600 s) get a snapshot (accept→Ready measured at ~60 s, a docker commit), then the `chat_sandbox_snapshots` row is upserted (chat_id is the primary key) and the wiped container goes back to the idle pool; at most 3 concurrent parks per round to protect the docker daemon. **This is the only path that reclaims a chat sandbox** — a second 600 s reaper used to wipe and release without snapshotting, and being the shorter threshold it always won, so files were lost with no backup to restore from; the two are now one;
- **Restore**: `_create_session_for` checks the DB snapshot **first** (~15–20 s restore, still faster than a fresh create), falling back to the Q2 user idle pool (~7 s warm path) only for chats with nothing to restore, and creating fresh last. The order matters — idle-pool containers are wiped clean, so handing one to a chat that has a snapshot silently drops its files. When booting from a snapshot the volumes **must be re-declared** — docker commit does not preserve mount configuration, otherwise bind mounts like `/workspace/skills/` are lost;
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

Skill files are exposed inside the sandbox at `/workspace/skills/<id>` through read-only bind mounts (`_make_skills_volumes`). What is bound there is **that user's own skill view**, `$HOST_STORAGE_PATH/sandbox_skills_u/<user_id>`: their private skills plus relative symlinks to the shared ones, with the shared skill dir `$HOST_STORAGE_PATH/sandbox_skills` mounted alongside at `/workspace/skills_shared` so those symlinks resolve (built-ins synced in at startup, DB skills materialized on demand — see the [skill system](agent-skills.md)). Every skill therefore keeps the same in-sandbox path, while another user's private skills (and their `secrets.json`) are simply not mounted. Pre-warmed light sandboxes are handed to whoever asks next, so they only ever mount the shared dir. Read-only guarantees skills cannot be tampered with from inside the sandbox; the directory bind is live, so newly imported skills are visible immediately. Without `HOST_STORAGE_PATH` it falls back to mounting only the built-in source tree, with a warning.

## Offloading oversized tool results

`CompactingAgent` keeps a bounded excerpt of oversized tool output and uses
`SandboxOffloader` to save the **complete text** under `.offload/` in the current
tool workspace. Images remain image inputs and are excluded from text offloads.
The path uses the same session as `Read` and `bash`:

- Local mode: `<local workspace>/.sessions/<session hash>/.offload/`, without a cloud upload.
- Cloud `script_runner`: `/workspace/.sessions/<session hash>/.offload/`, or the
  equivalent location under a configured workspace root.
- OpenSandbox / Cube (EE): `/workspace/.offload/` inside the current session sandbox.

Each write gets a unique filename so concurrent tools, retries and child agents
cannot overwrite earlier results. The model receives a real path only after a
successful write and should read pages or search selectively instead of loading
the whole large file into context again. Permission errors, unavailable services,
missing persistent sessions and timeouts leave the excerpt intact and explicitly
state that the complete output was not saved. The notice suggests a narrower or
paginated query; it never presents error text as a filename. Offload waits are
bounded to 30 seconds, and cancellation still propagates.

Context compaction and its SDK fallback use the same failure handling: a failed
archive does not undo successful compaction or advertise a nonexistent history
file. History archives are readable text; images and model reasoning are excluded,
so they do not replace the original conversation record. Files follow the current
workspace lifecycle and are not independent permanent backups.

Deploy the cloud backend and the desktop bundle's local backend to receive this
fix on both execution surfaces. Updating only the cloud does not fix old clients.

## Administrator sandbox management

- **Read-only monitoring (security console)**: `api/routes/v1/config_security.py` exposes `/v1/config/security/sandbox/*` — overview, instance list, per-instance detail, snapshot list, rebuild history and effective configuration; everything goes through the providers' `admin_*` interfaces, with the UI trimmed by each provider's `admin_capabilities()` declaration (script_runner can't enumerate instances, so those columns are greyed out).
- **Dependency rebuild (Enterprise EE)**: `api/routes/v1/admin_sandbox.py` (`/v1/admin/sandbox/*`) aggregates the pip/apt dependencies declared by all skills (`core/services/skill_deps_aggregator.py`) and lets an admin trigger a sandbox image rebuild with one click: the `script-runner` / `opensandbox` targets run a local `docker compose build`, while the `cube` target rebuilds the template on the remote node over SSH and hot-swaps it (`core/services/sandbox_rebuild_service.py` + `cube_template_builder`), with per-run status and logs. New skill dependencies get baked into the images without hand-editing Dockerfiles.

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `SANDBOX_PROVIDER` | `script_runner` | Provider selection: `script_runner` / `opensandbox` / `cube` |
| `SANDBOX_RUNNER_URL` | `http://hugagent-script-runner:8900` | script_runner sidecar address |
| `SANDBOX_ARTIFACT_MAX_BYTES` | `104857600` | Single switch for sandbox file size (100 MiB): fetch, auto-collected artifacts, push-in, and `/myspace` write-back share it |
| `OPENSANDBOX_DOMAIN` / `OPENSANDBOX_API_KEY` / `OPENSANDBOX_IMAGE` | — | OpenSandbox server & image |
| `OPENSANDBOX_POOL_{JUPYTER,LIGHT}_{MIN,MAX}_IDLE` / `OPENSANDBOX_POOL_MAX_TOTAL` | 2/3, 2/5, 20 | Warm-pool watermarks |
| `SANDBOX_IDLE_TTL_S` | 3600 | The one sandbox lifetime parameter: server-side TTL = idle threshold = keepalive basis; snapshot first, release second |
| `OPENSANDBOX_SNAPSHOT_ENABLED` | true | Snapshot system master switch |
| `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS` | 7 | Snapshot retention |
| `OPENSANDBOX_SNAPSHOT_WAIT_TIMEOUT_S` | 120 | Max wait for snapshot Ready |
| `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED` | true | Plan F myspace direct-mount switch |
| `HOST_STORAGE_PATH` | — | Real host path of the storage volume (bind-mount source) |
| `SANDBOX_SKILLS_DIR` | `$STORAGE_PATH/sandbox_skills` | Unified skills directory override |
| `MYSPACE_WRITE_CONFIRM` | true | Hard user-confirmation gate for /myspace writes |
| `CUBE_API_URL` / `CUBE_API_KEY` / `CUBE_TEMPLATE` / `CUBE_API_SANDBOX_DOMAIN` | — | Cube node connection |
| `CUBE_POOL_MIN_IDLE` / `CUBE_OWNER_TAG` | 2 / — | Cube pre-warm, ownership tag for shared nodes (reclaim timing also comes from `SANDBOX_IDLE_TTL_S`) |
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
| `src/backend/core/llm/tools/sandbox_tool.py` | bash / write_stdin / sandbox_put_artifact / sandbox_get_artifact |
| `src/backend/core/llm/offloader.py` | Overflow offloading to the session workspace |
| `src/backend/api/routes/v1/admin_sandbox.py` | Dependency-rebuild admin API (EE) |
| `src/backend/api/routes/v1/config_security.py` | Security-console read-only sandbox views |
| `src/backend/core/services/sandbox_rebuild_service.py` | Image/template rebuild orchestration (EE) |
| `docker/Dockerfile.script-runner` / `docker/Dockerfile.opensandbox` / `docker/Dockerfile.cube-sandbox` | The three sandbox images |

Related docs: [Agent skills](agent-skills.md) · [MCP tool system](mcp-tools.md) · [Projects & My Space](projects-myspace.md) · [Editions & licensing](../editions/overview.md)
