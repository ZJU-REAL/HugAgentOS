# Sandbox Execution System

> Last updated: 2026-10-18

The sandbox is the isolated environment where HugAgentOS's agents execute code: every `Bash` call the model makes in a conversation, every [skill](agent-skills.md) script run, every generated deliverable happens inside the sandbox rather than the backend process. A single **provider protocol** abstracts three interchangeable execution backends — from the single-host lightweight script_runner, through OpenSandbox with persistent sessions and snapshot recovery, to a remote MicroVM fleet (Cube) — with the tool layer above completely agnostic to the choice.

OpenSandbox execution, session files, personal mounts and snapshots are shared by CE and EE. Docker CE defaults to OpenSandbox. Team mounts, organization governance and Cube belong to EE. No-Docker installation and desktop local mode keep local execution.

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

Internal business code calls await provider.run_to_completion(request) to receive a final ProcessResult (stdout, stderr, exit_code, execution_time_ms), without managing process IDs or invoking model tools. All three providers share process management and result collection; waiting stays inside the provider, with one launch and no automatic retry. The model tool surface remains Bash / write_stdin. Commands have no default execution deadline; callers can explicitly set timeout. Individual waits, network request timeouts, and cloud sandbox lifetimes are separate limits.

Two key fields on `ProcessRequest`:

- **`session_id`**: every provider uses it to route the conversation workspace; calls with the same ID retain `/workspace` files, while different IDs cannot see each other. OpenSandbox and Cube additionally reuse the underlying container, MicroVM, or kernel;
- **`user_id`**: enables myspace file visibility (bind-mount or seeding) — see Plan F below.

## The three provider implementations

| Provider | File | Form | When to use | Edition |
|---|---|---|---|---|
| `script_runner` | `script_runner_provider.py` | One sidecar with persistent per-`session_id` filesystem directories; each command still runs in a setrlimit subprocess | Single-host lightweight execution; optional | CE |
| `opensandbox` | `opensandbox_provider.py` + `_opensandbox_*.py` | Docker containers, execd commands, session files, warm pools and snapshots; Python/JS variables are not retained | Docker CE default | CE / EE |
| `cube` | `cube_provider.py` | Tencent CubeSandbox (E2B-compatible MicroVMs) on a **remote node** — the backend reaches it over the network via the `e2b_code_interpreter` SDK, no local sidecar | Deployments where the backend host is resource-constrained, stronger (MicroVM-grade) isolation is required, or sandbox compute must scale independently | **EE** |

Select the provider with `SANDBOX_PROVIDER` and match `COMPOSE_PROFILES`. Missing modules or SDKs raise an error; the factory never changes the selected provider.

Cube's design trade-offs (the price of being remote): every language goes through "write a script file + `commands.run`" without Jupyter; **no host bind mounts** (myspace files are materialized via `put_file` by the tool layer, and skill files matching `/workspace/skills` are pushed at runtime, governed by `CUBE_SKILL_PREPUSH*`); no snapshot system; `session_id` still binds a persistent MicroVM (create on first use, connect-reuse afterwards).

## Agent-side tools

`core/llm/tools/sandbox_tool.py` registers process and artifact tools with the agent (their `register_*` functions are invoked by `factory/tools/native.py`):

| Tool | Purpose |
|---|---|
| `Bash(command, timeout=None, yield_time_ms=60000, run_in_background=False)` | Start a command and wait 60 seconds by default; unfinished commands enter background management and return a managed task ID. Set run_in_background=True to hand off immediately. Omitted timeout sets no command deadline; the command tool uses the name `Bash` with an uppercase B |
| `write_stdin(session_id, chars="", yield_time_ms=0)` | Read new output immediately by default; optional waits stop at 3000 ms. Completion automatically notifies the chat. Ctrl+C (\u0003) requests interruption |
| `background_process(session_id)` | Adopt the original process handle without restarting it |
| `list_processes()` | List this user's tasks in the current chat |
| `sandbox_put_artifact(artifact_id, dest_path)` | Copy a platform artifact's bytes (user uploads, chart-tool outputs, …) into a sandbox path — uploads are never auto-visible in the sandbox |
| `sandbox_get_artifact(src_path)` | Stream a sandbox file into a downloadable artifact; the default per-file limit is 100 MiB — Bash outputs never auto-appear in the attachment area |


Execution deadlines and wait windows are separate: Bash initially waits 250–60000 ms. Ordinary-chat write_stdin defaults to zero wait and caps optional waits at 3000 ms. Provider operations and evaluation tools retain their own wait contracts. A wait expiring never terminates the command. Explicit `timeout` remains an execution deadline and is no longer clamped to 120 seconds. All commands and internal Python/JavaScript scripts use the process API. The synchronous execution endpoint and default/maximum execution timeout settings have been removed. Process IDs are bound to the initiating user and conversation. This is non-PTY execution with closed stdin: only empty input and Ctrl+C are accepted. On headless Windows, interruption terminates the process tree.

The runtime retains at most 64 process records and only evicts completed records. Pending output is bounded; `output_omitted_chars` reports omissions. Local/script_runner logs remain in the conversation workspace at `.__process_*/stdout.log` and `stderr.log`; `output_files` returns their real readable paths. A stream exceeding 64 MiB stops the command and preserves the written log. OpenSandbox background logs merge stdout/stderr; Cube commands stop above 64 MiB total output. Active commands periodically touch their sandbox: OpenSandbox can renew its lease, while compatible Cube servers may not support extending their server-side TTL; `lifetime_note` reports that limitation. The ordinary-chat ledger persists task state and bounded output. OpenSandbox restores the original execution and committed log cursor. Other providers stop commands on orderly shutdown and report unknown after an unexpected restart. Explicit chat deletion stops its commands. Team source changes synchronize when terminal output is collected; running does not mean saved.

The sandbox identity is resolved by `resolve_sandbox_session(sandbox_session_id, chat_id)`: a non-empty explicit ID wins, while a missing or empty value falls back to `chat_id`. The main agent, plan execution, batch items, and every subagent therefore use one conversation sandbox, and a child run never destroys it when it finishes.

**MySpace real-time registration**: a file written under `/myspace` **is** a file in the user's My Space, and both sides must show the same state at any moment. Registration is driven by **the filesystem itself** (`core/files/spaces/`): any write or delete under the mirror directory `myspace_cache/{uid}/` is registered back into the artifact ledger, **regardless of who wrote it**.

The earlier design had each writing tool register its own change (write / edit / file delete-move-mkdir / a before-and-after directory snapshot around `Bash`). That assumes changes can only arrive through those entry points, and they cannot: a `nohup`-ed background process writes after the command returns, sub-agents and batch runs write from another coroutine, skill CLIs write directly, and so do MCP servers. Every entry point left out shows up the same way — the file sits on the user's disk, is invisible and undeletable in the UI, and every new sandbox mounts that directory again, which is where "leftover intermediate files from the previous session" came from.

| When | Direction | What happens |
|---|---|---|
| A write or delete in the directory | Sandbox → My Space | Registered as an artifact, or the deleted file is soft-deleted from My Space |
| Before every `Bash` | My Space → sandbox | UI uploads/edits land in the mirror directory (immediately visible under the bind mount); files deleted in the UI are removed from the mirror |

Registration splits by the nature of the file — it is **not** a blanket push:

- **New files** (no artifact record yet) → registered and shown directly, without interrupting the user. The bytes already sit on the user's disk, so blocking here would only mint invisible-but-undeletable files that the next session still mounts.
- **Edits to / deletions of the user's existing files** → routed through the `MYSPACE_WRITE_CONFIRM` gate. The file on disk has already changed, so the gate cannot block; what it does is **restore that version from object storage** when the user declines. Where nobody can answer (no live chat, sub-agents, batch runs, scheduled tasks) the change applies directly instead of being denied — denial cannot undo what already happened, it only leaves the two sides inconsistent. The gate also honours the user's approval preset, so "full access" no longer prompts per file.
- **Files the user deleted are never resurrected**: leftover mirror copies are never registered — the forward pass removes them instead. Only a same-named file written *after* the deletion counts as new content.

Other points:

- **Registration attaches no chat, and pins no workspace card**: MySpace is one per-user directory that every session's sandbox mounts, so a filesystem event carries a path and no chat identity — who wrote it is simply not knowable here. Registration therefore records only the owning user (`Artifact.chat_id` stays null), and the registry never pins: a card can only be pinned by the run that wrote the file (the model calling `pin_to_workspace`, or that run's own Write / delivery guard — all through the `core.llm.runtime.workspace` ContextVar), where both "which chat" and "which file" are certain. Picking "whichever run of this user happens to be live" instead used to file one chat's output under another chat and surface it in that chat's workspace.
- **The reference point is the artifact record, not the mirror cache**: with the bind mount on, the sandbox's `/workspace/myspace/{uid}` *is* the backend's `myspace_cache/{uid}`, so "sandbox file matches mirror cache" is trivially true and can no longer mean "already registered". That mismatch used to make write-back a permanent no-op.
- **"Newer on disk than in the ledger" is the only signal**: when the backend writes into the mirror itself (materialising a UI upload, restoring a declined change) it aligns the file's mtime to the registration time, so the watcher never mistakes the backend's own write for a fresh sandbox change — no separate "I wrote this" bookkeeping is needed.
- **Every worker watches; a claim deduplicates**: the confirmation bar lives in the process running that chat, so the watcher has to run in every worker (the cost is inotify watches scaling as workers × directories against `fs.inotify.max_user_watches`; raise it on deployments with very many folders — exceeding it drops events **silently**); one change is still handled once, via the claim in `core.infra.ephemeral`, and preferably by the worker hosting that user's chat (only it can raise the bar).
- **When the sandbox directory is not on this host** (`script_runner` / `cube`: the session workspace lives inside a container, or the whole sandbox is remote), `core/files/spaces/sandbox.py` copies the sandbox's current state into the mirror directory after the command, and everything downstream is identical.

### Large artifact delivery

`sandbox_get_artifact` supports individual files up to 100 MiB (104,857,600
bytes) by default. `SANDBOX_ARTIFACT_MAX_BYTES` is the **single switch** for
sandbox file size, bounding all four paths: explicit `sandbox_get_artifact`
fetches, artifacts auto-collected after a `Bash` run (both per-file and
per-batch total), artifacts pushed into the sandbox, and the `/myspace`
write-back sync after a `Bash` run; the backend and the script-runner sidecar
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

## OpenSandbox session lifecycle (CE / EE)

```text
Request → authorize identity and session binding → reuse / restore / create container
        → execd starts a separate process → retain files → renew on activity
Idle reaches SANDBOX_IDLE_TTL_S → create snapshot → wait Ready → record in database
        → clean workspace → return to the same user's idle pool
User returns → restore the session snapshot first → declare mounts again → run command
```

- A conversation reuses container files. Each Python/JS command starts a separate process and retains no variables.
- A user-bound container can only be reused by the same user, after clearing the previous conversation workspace.
- The base runtime preserves personal and skill mounts. EE adds team mounts and permission checks.
- Docker CE does not prewarm idle containers by default. Historical jupyter setting names refer to the user-session pool; no Jupyter server starts.
- SANDBOX_IDLE_TTL_S sets the idle snapshot threshold, default 3600 seconds. Hard server expiry adds grace for the reclaim queue, renewal skew, snapshot request and readiness wait. Each scan renews queued sessions before fairly scheduling snapshot batches.
- Authenticated renewal and snapshot requests share the OPEN-SANDBOX-API-KEY header.

## Snapshot persistence (CE / EE)

Implementation: core/sandbox/providers/opensandbox/snapshots.py. Goal: stop idle sessions from squatting on Docker resources while letting users resume **with their filesystem intact**.

- **Park**: a background worker scans every 60 s; sessions idle beyond `SANDBOX_IDLE_TTL_S` (default 3600 s) get a snapshot (accept→Ready measured at ~60 s, a docker commit), then the `chat_sandbox_snapshots` row is upserted (chat_id is the primary key) and the wiped container goes back to the idle pool; at most 3 concurrent parks per round to protect the docker daemon. **This is the only path that reclaims a chat sandbox** — a second 600 s reaper used to wipe and release without snapshotting, and being the shorter threshold it always won, so files were lost with no backup to restore from; the two are now one;
- **Restore**: `_create_session_for` checks the DB snapshot **first** (~15–20 s restore, still faster than a fresh create), falling back to the Q2 user idle pool (~7 s warm path) only for chats with nothing to restore, and creating fresh last. The order matters — idle-pool containers are wiped clean, so handing one to a chat that has a snapshot silently drops its files. When booting from a snapshot the volumes **must be re-declared** — docker commit does not preserve mount configuration, otherwise bind mounts like `/workspace/skills/` are lost;
- **Single-use**: once a snapshot has been consumed as a boot image it is marked for 1-hour short retention (an immediate DELETE would 409 because the new container still references the image layer);
- **GC**: an hourly sweep deletes expired snapshots (DB row + remote; default retention `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS=7`), retrying delete conflicts on the next round;
- master switch `OPENSANDBOX_SNAPSHOT_ENABLED` (default true); off reverts to the old "idle = gone" behaviour.

## MySpace bind mount (CE / EE)

With `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED=true` (the default), User-session sandbox creation bind-mounts the host's `$HOST_STORAGE_PATH/myspace_cache/{uid}/` directly into the sandbox at `/workspace/myspace/{uid}/` (`_opensandbox_internals.py::_make_myspace_volume`):

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
The path uses the same session as `Read` and `Bash`:

- Local mode: `<local workspace>/.sessions/<session hash>/.offload/`, without a cloud upload.
- Cloud `script_runner`: `/workspace/.sessions/<session hash>/.offload/`, or the
  equivalent location under a configured workspace root.
- OpenSandbox (CE / EE) and Cube (EE): `/workspace/.offload/` inside the current session sandbox.

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

- **Read-only monitoring (security console)**: `features/security_console/api/config_security.py` exposes `/v1/config/security/sandbox/*` — overview, instance list, per-instance detail, snapshot list, rebuild history and effective configuration; everything goes through the providers' `admin_*` interfaces, with the UI trimmed by each provider's `admin_capabilities()` declaration (script_runner can't enumerate instances, so those columns are greyed out).
- **Dependency rebuild (Enterprise EE)**: `features/sandbox_ops/api/admin_sandbox.py` (`/v1/admin/sandbox/*`) aggregates the pip/apt dependencies declared by all skills (`core/capabilities/skills/deps/aggregator.py`) and lets an admin trigger a sandbox image rebuild with one click: the `script-runner` / `opensandbox` targets run a local `docker compose build`, while the `cube` target rebuilds the template on the remote node over SSH and hot-swaps it (`features/sandbox_ops/rebuild.py` + `cube_template_builder`), with per-run status and logs. New skill dependencies get baked into the images without hand-editing Dockerfiles.

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `SANDBOX_PROVIDER` | CE Docker: `opensandbox` | Provider selection: `script_runner` / `opensandbox` / `cube` |
| `SANDBOX_RUNNER_URL` | `http://hugagent-script-runner:8900` | script_runner sidecar address |
| `SANDBOX_ARTIFACT_MAX_BYTES` | `104857600` | Single switch for sandbox file size (100 MiB): fetch, auto-collected artifacts, push-in, and `/myspace` write-back share it |
| `OPENSANDBOX_DOMAIN` / `OPENSANDBOX_API_KEY` / `OPENSANDBOX_IMAGE` | — | OpenSandbox server & image |
| `OPENSANDBOX_POOL_{JUPYTER,LIGHT}_{MIN,MAX}_IDLE` / `OPENSANDBOX_POOL_MAX_TOTAL` | 2/3, 2/5, 20 | Warm-pool watermarks |
| `SANDBOX_IDLE_TTL_S` | 3600 | Idle snapshot threshold and keepalive basis; server expiry adds snapshot and queue grace |
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
| `src/backend/core/sandbox/factory.py` | Explicit provider selection; initialization errors propagate |
| `src/backend/core/sandbox/providers/script_runner/provider.py` | Lightweight sandbox (CE) |
| `src/backend/core/sandbox/providers/opensandbox/provider.py` | Shared OpenSandbox provider |
| `src/backend/core/sandbox/providers/opensandbox/` | Sessions / snapshots / park-restore workers (EE) |
| `src/backend/core/sandbox/providers/opensandbox/execution.py` | Execution path + idle reaper (EE) |
| `src/backend/core/sandbox/providers/opensandbox/` | Volume builders, metadata, user pool (EE) |
| `src/backend/core/sandbox/providers/opensandbox/pool.py` | Two-bucket warm pool |
| `src/backend/core/sandbox/providers/cube/provider.py` | Cube remote-MicroVM provider (EE) |
| `src/backend/core/llm/tools/sandbox_tool.py` | Bash / write_stdin / sandbox_put_artifact / sandbox_get_artifact |
| `src/backend/core/llm/context/offloader.py` | Overflow offloading to the session workspace |
| `src/backend/features/sandbox_ops/api/admin_sandbox.py` | Dependency-rebuild admin API (EE) |
| `src/backend/features/security_console/api/config_security.py` | Security-console read-only sandbox views |
| `src/backend/features/sandbox_ops/rebuild.py` | Image/template rebuild orchestration (EE) |
| `docker/sandbox/Dockerfile.script-runner` / `docker/sandbox/Dockerfile.opensandbox` / `docker/sandbox/Dockerfile.cube-sandbox` | The three sandbox images |

Related docs: [Agent skills](agent-skills.md) · [MCP tool system](mcp-tools.md) · [Projects & My Space](projects-myspace.md) · [Editions & licensing](../editions/overview.md)

Each Docker OpenSandbox instance has its own bridge, joined only by the trusted backend and controller, with no published sandbox host ports. User isolation checks cover personal mounts, session permissions, and blocked access to another sandbox's execd.

### Ownership checks during object mount recovery

Sandbox creation and controller recovery share the user label defined in
`core/sandbox/sessions/metadata.py`. The controller image copies this module at build time.
Product ports must retain their existing label value and rebuild the controller.
Do not change only one side or accept another product's label to bypass ownership.
Recovery still checks the current user, permission revision, mount scope,
credentials, and read-only attributes. Authorization conflicts return 409 and
retain the sandbox and workspace. Mount service failures return 503.
After a controller upgrade, verify existing sandbox recovery and cross-user
denial before running user commands.

## Background Bash in ordinary chats

Bash accepts `run_in_background=True`. A command also enters background management if its foreground wait ends before process exit. The runtime starts each command once. `background_process(session_id)` adopts an existing handle. It does not execute the command again. `list_processes()` lists the current user's tasks in the current chat.

The backend collects status and logs and keeps the sandbox active. The model can do independent work. If it has no other work, the current model run ends. Completion enters the next reasoning step in an active run. If the chat is idle, the existing run admission and execution path starts a follow-up in the original chat.

`write_stdin` reads new output, waits briefly, or sends Ctrl+C. The model must not poll for completion. Authenticated log API reads do not consume the model's log cursor. The chat has no separate background-command strip. The page keeps /v1/commands/events open before wakeup. The backend pushes the ready run ID. The page immediately joins its existing output stream without task polling. Reconnection sends a snapshot of ready runs. Completed short replies load from history. A task remains managed until process exit is confirmed. Deleting the chat or revoking edit access stops its commands.

The command ledger stores bounded logs, execution leases and completion delivery state. OpenSandbox recovery uses the original execution identity, sandbox binding and committed log cursor. It never restarts the command. Other providers stop owned processes on orderly shutdown. After an unexpected restart, an unrecoverable execution is reported as `unknown`.

Migration `bashbackground01` creates the command ledger. Apply it through the normal release procedure before using the feature. Tests use isolated databases and disposable sandbox sessions.

### Reclaim accounting and multi-worker administration

When the controller explicitly returns DOCKER::SANDBOX_NOT_FOUND, the backend
removes the missing session and returns its reservation exactly once.
Snapshot or renewal timeouts, authentication failures, ordinary route 404s and
server errors do not authorize destruction of a live container. Workspace reuse
requires a successful snapshot. Explicit close and idle reclaim share session
locks and distributed claims.

OpenSandbox instance lists and counts use the controller's current running
inventory. Controller query failures mean
the count is unavailable, never a successful zero. Pool diagnostics include
scope=worker and worker_pid. They describe one backend process and are not a
global per-user quota. The backend admission ledger enforces the shared capacity described below.

## Shared capacity and maintenance ownership

Every OpenSandbox creation goes through `providers/opensandbox/admission.py`; a
guard test rejects any other `Sandbox.create` call. Admission takes one of
OPENSANDBOX_POOL_MAX_TOTAL slots in the shared ephemeral store (Redis when
configured), then one of OPENSANDBOX_CREATE_CONCURRENCY creation gates, and only
then calls the controller. Each slot is claimed with an atomic put-if-absent, so
over-admission cannot happen and no global lock exists. The controller performs
Docker operations only.

- A request never fails because another session is creating. Interactive
  creations wait up to OPENSANDBOX_ADMISSION_WAIT_S on the event loop; pool
  pre-warm never waits and never takes the last OPENSANDBOX_USER_HEADROOM slots.
- The ledger is trusted only right after reconciling with the controller's
  inventory: before the first admission, at least every minute from pool
  maintenance, and — completed after the request began — before admission
  reports exhaustion. Reconciliation
  frees slots of vanished sandboxes, accounts sandboxes nobody holds a slot for
  (a creator that crashed after the controller answered), and resolves creations
  whose outcome was unknown (timeouts, cancellation) by their admission token.
- Every uncertainty keeps a slot rather than freeing one. Releasing on delete is
  an optimisation; a kill path that forgets it delays the slot until the next
  reconciliation and never over-admits.
- `SandboxCapacityExhausted` (user-facing "沙箱资源已满") means a fresh
  reconciliation confirmed every slot is in use.
- Personal Agent computers run on their own controller and use their own ledger
  sized by PERSONAL_AGENT_SANDBOX_MAX_TOTAL.

The controller makes Docker enumeration tolerate concurrent removal process-wide
(`opensandbox_inventory.py` defaults `containers.list(ignore_removed=True)`), so
listing, startup recovery and failure cleanup no longer fail with 404 when a
container disappears mid-enumeration.

The leader discovers idle sessions from shared bindings, including sessions
created by other workers or surviving a restart. Identity-checked cleanup of a
missing binding does not block other sessions. Snapshot work stops with leadership;
GC deletes metadata only for the snapshot_id it processed.

Output reads no longer prepare mounts or write object directory markers.
Directory initialization happens during creation, while execution and file access
still check authorization. An uncertain or timed-out health probe preserves the
FUSE helper; only confirmed missing or disconnected mounts permit replacement.
Command observation retries are bounded before durable recovery takes over.
Processes without a recoverable reference require confirmed termination, not loss
of their control handle merely because observation failed.

Deploy the backend and the controller image together: the controller does not
limit creation, so capacity holds only while every backend admits through the
ledger.

Skill mounts require HOST_STORAGE_PATH to identify the host directory corresponding to backend STORAGE_PATH. Missing configuration fails explicitly; source-tree fallback is removed. Restart adoption requires explicit ownership metadata and never infers ownership from old entrypoints.
