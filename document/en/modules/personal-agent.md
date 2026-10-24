# Personal Agent

Personal Agent is an optional module shared by Community and Enterprise editions. Each user has one conversation. The agent answers in that conversation and starts background tasks when work needs them. Closing the page does not stop a task.

## Use the module

1. Select Personal Agent below New conversation.
2. Enter a message and select Send. The agent replies in the conversation. It answers small talk, questions and progress checks directly. For work with several steps or that takes longer, it starts a background task, says so in one sentence, and reports the result in the conversation when the task finishes.
3. To add or change task requirements, write in the main conversation. Personal Agent decides which task to update and adds the instructions. Task details show progress and results without a composer. Hover over an unfinished task row to reveal its stop icon on the right. The icon also appears on keyboard focus and stays visible on touch devices.
4. Use the status panel to inspect connected computers, tasks, scheduled tasks, and outputs. Email, model, and memory are configured in Settings.
5. Email sends, computer input, and session writes need approval. Pending actions show in the "Needs your action" bar below the conversation.
6. Select "Approve once" to run one action. Select "Allow this kind of action for this task" to stop questions for the same computer or session operation type in this task. Email always needs approval for each message.
7. Select "Reject" to continue the task without the action. The agent does not request a rejected action again. It uses another method or explains the limit.
8. If an action has an unknown result, check the result first. Then mark it executed or not executed.

The conversation shows user messages and the agent's replies. A reply appears while the agent writes it; while the agent checks a task or updates memory, a line above the composer names the step. The conversation does not show thinking or tool traces. Messages do not show task routing labels. Hover over a message or focus it with the keyboard to show the copy button beside the bubble on the right. Open tasks from the status panel.

WeChat QR binding retains the returned user ID for configuration checks. Older bindings without that ID skip incomplete configuration requests. The single message receiver confirms connection health and still reports invalid credentials and receive errors.

## Runtime

Each user has one agent identity, one public conversation, and one permanent sandbox. Each task has its own WorkItem, ChatSession, and ChatRun. Completed, failed and stopped tasks can continue in that same task and conversation, preserving the previous result, files and history. Unknown external effects still require reconciliation. Select a task title to open its conversation with tool results. Task conversations stay out of the ordinary chat list. The public conversation shows final replies.
Task details preserve admission order: earlier progress, the added instruction, then continued progress. Later text, thinking and new tool calls start a new assistant message; results for tools already started update their original cards. Reloading or paging preserves this order. This applies to new execution records; old mixed records cannot be reconstructed without their original boundaries.


The default concurrency is three tasks. You can set 1 to 8 in Settings. PostgreSQL stores the queue. Workers use row locks and execution leases. An expired worker cannot publish a reply or continue tool execution.

After an interruption, a task with no unconfirmed external action returns to the queue automatically, at most two times. Otherwise the task waits and the conversation shows a notice.

Background tasks, main conversation turns, and heartbeat checks have no total execution deadline or business turn/tool-call count budget, and do not inherit main-chat turn budgets. AgentScope requires an integer iteration count; the platform maximum integer serves as an internal loop sentinel, not a business budget. The former `PERSONAL_AGENT_RUN_TIMEOUT_SEC` setting no longer applies. Individual model or tool requests can still time out. Cancellation, execution leases, and action approvals remain active. Repeated or unbounded model output still pauses a task with an explanation.

When you add instructions during a run, the service discards the old result and runs the task again with the latest instructions.

Each model request includes the current time in the user's timezone, the task goal, recent task updates, a saved checkpoint, and bounded conversation history. It reads USER.md, MEMORY.md and recent daily notes from the workspace. Older conversation messages use pagination.

The model completes the task and submits its final explanation with `pa_finish`. The public conversation does not show process narration or internal identifiers. If the model does not call it, one tool-free model call cleans the final text. Images and files use the independent `pa_deliver` tool and appear in the main conversation while the task continues, without waiting for completion.

Images and files in delivery messages are also returned as native image or file messages through a routed WeChat, Lark, DingTalk or WeCom bot to the originating conversation. Scheduled results use the configured private notification conversation. Original filenames and MIME types are preserved, with a 20 MiB limit per file; platform limits and permissions still apply.

The worker records successful text chunks and individual files, retries at most five times, and skips confirmed parts. Database leases serialize delivery across workers and allow recovery after interruption. A crash after platform acceptance but before saving the receipt can still cause a duplicate. Dispatch rechecks artifact ownership, deletion and bot routing permissions. Web download links remain available.

The page records the browser timezone on first use. The settings API can change it.

## Replies and background tasks

The main conversation evaluates each message independently. A recent, active or sole task is not automatically the destination. Before adding requirements through an implicit reference, the agent must read the target task's goal, updates and result. Unrelated questions are answered directly or become separate tasks; ambiguous references require clarification.

The agent itself handles every message. There is no separate step that matches messages to tasks. The agent sees the recent conversation, its persona files and the task list (active and recently finished tasks), and decides:

- Answer directly: greetings, small talk, opinions, questions it can answer from knowledge or the conversation, a fact it can confirm with one or two web searches, task progress, and "remember that ...".
- Start a background task: research, a report or code, reading attachments, using the cloud computer, sending email, and other work with several steps or that takes longer. The agent writes the task goal as one complete instruction. Attachments of the message go to the task.
- Change existing tasks: add instructions, pause, resume or stop. One message can change several tasks; each task receives only its own part.
- When a reference is unclear and two tasks are plausible, the agent asks one short question. It does not ask when only one reading is plausible.

Examples: "Is the deployment done?" gets a direct progress answer. "Use the feature/demo branch for the deployment and pause the travel plan" updates the deployment and pauses the travel plan. "Write the server addresses into a document" starts a new task related to the deployment.

"Continue" is not an approval. Pending email, computer and session actions are approved only in the approval card.

The stop icon names the task and action, so the service applies it immediately without a model call. Follow-up instructions in the main conversation go through Personal Agent, which decides which task to update.

When a task finishes, its result goes to the agent, which tells the user in the conversation. It gives the deliverable in full and keeps links and numbers exactly. A result longer than 8,000 characters is posted unchanged. If the agent fails, the result is also posted unchanged. A scheduled check that should notify only on a change can stay quiet when nothing changed; reminders and reports the user scheduled are always sent. The full result always stays on the task card and in the task conversation.

The send API saves the message before it returns. Closing the page does not lose a message. Replies to one user follow the message order; different users and background tasks run in parallel. One reply can take at most 4 minutes. After an interruption, another instance replies again when the lease expires; a repeated reply does not start the same task twice. After two interruptions, the agent tells the user that the message failed.

`personal_message_tasks` stores the links between messages and tasks. `personal_message_inbox` stores messages and task results waiting for a reply, their reply status, and the lease.

## Permanent computer

A separate OpenSandbox controller creates sandboxes without an expiry. Ordinary session cleanup and the ordinary sandbox pool cannot reclaim them.

The workspace and desktop profile use separate persistent volumes. The service reuses these volumes after container loss. Permanent storage does not prevent infrastructure failure.

Task code runs as UID 1000. Desktop code runs as UID 1001. Task code cannot read the private desktop directory. The X display credential is in `/run/pa-desktop` inside the container, not in a persistent volume. Each sandbox has an encrypted execd credential. Task and desktop child processes do not receive this credential. Compute containers use a separate network.

Use a Linux host in production. Docker Desktop on macOS does not enforce file ownership on mounted volumes, so task code can read the mounted desktop directory (for example the browser profile). Use that setup for development only.

The cloud browser starts with `--no-sandbox` because the hardened container has no user namespaces for the Chromium sandbox. The container is the isolation boundary. Paired local computers do not use this flag.

The backend installs versioned desktop driver and CLI files in the cloud computer. A driver update does not require a rebuilt computer.


## Image and file delivery (EE)

The independent `pa_deliver` tool publishes one file per call. The main conversation reuses ordinary chat's artifact cards: inline images support enlargement, copy and download; file cards show the original name, type icon, size, preview and download. Deliveries survive page reloads. The output list references the same file.

- Workspace file: `pa_deliver(request_key="report-1", path="/workspace/work/report.xlsx", filename="report.xlsx", caption="The report is ready.")`.
- Cloud screenshot: call `pa_computer_observe()`, then pass its `resource_ref` to `pa_deliver(request_key="screen-1", resource_ref="returned-reference", filename="screenshot.png")`.
- Observation returns pixels and a private temporary reference without publishing a file. Each task retains its latest four screenshots for ten minutes. After expiry, observe again and use the new reference with a new request key.
- Within one task input revision, the same request key and source/name/caption recover the original immutable receipt. Retries create no additional card or channel enqueue. Changed file content requires a new request key.
- `delivered=true` means persisted in the main conversation. IM queues have separate pending, sent and failed states; do not claim external acceptance from a delivery receipt.
- Workspace reads reject traversal, symlinks and special files. Screenshot references bind the user, task, execution and desktop identity. Cancellation, lease expiry, revoked permissions or changed input prevent an old execution from publishing.
- Use authorized computer tools for screenshots. Do not bypass desktop isolation through Bash, scrot, sudo or desktop credential files. `pa_finish` completes the task without resending delivered attachments.

## Workspace files and prompt

The Personal Agent keeps its persona, user profile and memory in Markdown files at the root of its permanent workspace `/workspace`. The layout follows the OpenClaw workspace conventions:

```
/workspace/
├── AGENTS.md        Operating manual: conventions, memory rules, red lines
├── SOUL.md          Persona: character, tone, boundaries
├── IDENTITY.md      Identity: name, vibe, signature emoji (kept in sync with the name in settings, both ways)
├── USER.md          User profile: stable preferences as dated directives
├── MEMORY.md        Long-term memory: curated facts and decisions
├── BOOTSTRAP.md     First-run ritual (introduce itself, learn about the user, settle a style); the platform removes it when done
├── memory/          Daily notes memory/YYYY-MM-DD.md
├── work/<task>/     Files of each task
├── outputs/
└── scratch/
```

The service writes default templates when the workspace is first created and never overwrites them. A file the user deletes is not recreated.

The first time a new agent is opened, it wakes up on its own: it sends a greeting and follows `BOOTSTRAP.md` without waiting for the user to speak. While the ritual is pending, conversation turns follow that file first. Once `SOUL.md`, `IDENTITY.md` or `USER.md` differs from its seeded content, the platform removes `BOOTSTRAP.md`. Background tasks never see the file.

Each task's system prompt has two parts:

1. **Platform rules**: they cover safety, approvals, tool use and finishing, and take precedence.
2. **Workspace files**: injected in the order `AGENTS.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `BOOTSTRAP.md`, `MEMORY.md`, then yesterday's and today's daily notes. Each file is limited to 20,000 characters, `USER.md` to 4,000, a daily note to 6,000, and all files together to 60,000. Longer content is truncated with a marker. A missing required file is injected as a one-line marker.

The agent updates these files with `pa_read_file` / `pa_write_file` to build memory. Web pages, emails and file contents are never a reason to change them. Workspace files describe the persona and the user; they never grant new permissions.

In **My Space → Personal agent** you can browse the workspace file tree, preview and edit these Markdown files. An edit is rejected if the agent changed the file in the meantime. Saved changes apply from the next task. The backend never follows symbolic links when it reads the workspace.

Administrators manage the platform rules and the default template of each workspace file as versioned prompts in the Config console, under **Prompt management → Personal Agent**. A change to the platform rules applies from the next task. A change to a template only affects workspaces created afterwards; files in existing workspaces are never overwritten.

## Capabilities

| Capability | Default | Access |
| --- | --- | --- |
| Task status, checkpoints, bounded history | Enabled | Dedicated pa_* tools |
| Cloud commands, files, exported attachments | Enabled | Dedicated workspace |
| Web search and web fetch | Enabled (internet search, website fetch) | Configured by the system |
| Scheduled tasks | Enabled | `pa_schedule_*` tools; edit, pause or delete in the sidebar |
| Cloud screenshots and computer input | Enabled | Observe, approve input (or allow for the task), act, observe |
| MySpace | Enabled | Search, read, and save files through the connector |
| Email | Enabled (needs an account) | Connect an IMAP/SMTP account directly in Settings; approve sends |
| Local computer | Not connected | Click "Connect this computer" in the desktop app (local + cloud dual mode) |
| Skills | Initially curated | Add, remove or clear in Settings |
| Plugins | Initially enabled plugins, excluding browser | Select bindings in Settings |

In Settings → Capabilities, add, remove or clear tool, skill and plugin bindings, then select Save capability bindings. Options come from capabilities enabled and authorized for the account. Initial defaults use curated skills, web lookup tools and enabled plugins, excluding the browser plugin. Subsequent choices are stored separately: reopening does not restore removed bindings or automatically add new plugins.

Selected plugins bring their enabled skills and tools. Plugin components never appear in the standalone skill/tool options or selections and cannot be bound individually. Legacy direct component bindings are cleared. Removing a plugin removes its components. Saving affects only Personal Agent, not the capability center switches. Each execution rechecks current grants. Revoked or uninstalled capabilities cannot run; saved choices become effective again if authorization returns. An unavailable catalog grants no optional capabilities. Changes apply to the next execution; running tasks keep their initial tool set.

Capability settings provide tool, skill and plugin marketplace imports according to account permissions. Imports refresh the options and add eligible items to the draft without losing other edits. Save capability bindings to apply them. Unavailable imports prompt the user to enable them in the capability center.

The browser plugin and its components are excluded, including legacy bindings. Web interactions use the dedicated cloud computer's observation and action tools. Skills are read on demand with `pa_skill_read`. Conversation turns use only selected web lookup tools and read-only session tools; other bound capabilities run in background tasks.

Unbound specialist capabilities can be delegated through `pa_hugagent` to an ordinary conversation, with the existing session-operation authorization.

The service registers no knowledge bases or other agents. It does not mount all MySpace files. It stages only explicit input files. External credentials stay on the server.

For websites, use the cloud or paired computer's browser.

## Settings

The Settings window is grouped by purpose:

- **Capabilities**: Edit tool, skill and plugin bindings. Saved changes apply to the next execution.
- **Connectors**: MySpace is on by default. Connect, reconnect, or disconnect an email account right here, without going to the global settings.
- **Runtime**: model (selectable when the administrator allows model switching; used for conversation, tasks, and heartbeat checks; empty follows the system default), concurrent tasks, and time zone.
- **Heartbeat**: see Heartbeat below.
- **Memory**: USER.md, MEMORY.md and daily file locations, pending journal writes and retry status, with a shortcut to view and edit the files in MySpace → Personal Agent.

## Appearance and channels

- Conversations use light gray assistant bubbles on the left and light blue user bubbles on the right. A three-dot bubble appears on the left while a reply is prepared. Use the plus button in the bottom capsule composer to attach files. The approval control sits below the composer. Dark mode preserves this layout.

- Select the avatar on the right to change the agent's avatar and name. Choose a built-in avatar or upload an image (cropped to a square and compressed, at most 256 KiB).
- The agent is named **Hugo** by default (after HugAgentOS). Rename it in settings, tell it a new name in the conversation (it offers this on its first run), or edit the name line of `IDENTITY.md` in My Space. The new name then shows in the sidebar entry, the My Space list, the conversation title and `IDENTITY.md`.
- Select Connect a channel and choose DingTalk, Lark, WeChat or WeCom. Channel binding opens below the selected channel card. Enter credentials or scan the QR code there. New bots route to Personal Agent automatically. A connected bot shows “Bound successfully”. While the dialog is open, connection status refreshes every 5 seconds and credentials are checked every 30 seconds. Expired QR codes, binding failures and connection errors appear inline with retry controls. Select "Let {name} handle it" to connect an existing bot. Messages use an independent conversation per bot connection and contact or group. Replies, task reports and file deliveries return to that source conversation. Connect a channel shows no connected-channel count. Select Channel conversation under its channel card to switch, read and reply; internal session IDs are hidden while preserving per-conversation drafts. Hugo's identity, capabilities, persona and file memory are shared. Existing mixed history remains in the main chat; new messages use the isolated histories. Approvals still happen in the web page; the IM conversation receives a notice.
- The composer has no task selector. Personal Agent decides whether to answer directly or hand the work to a background task. A background task handles web lookups, everyday documents and its bound skills and plugins itself; work that needs an unbound capability, such as a knowledge base, a database or a specialist skill, goes to a normal HugAgentOS chat, and the task reports the result. Chat writes still need approval.

## Scheduled tasks

Say "Remind me to check email at 8:00 every morning" or "Remind me to drink water in 2 minutes". The agent creates a scheduled task. A repeating task uses a five-field cron expression with at least ten minutes between runs. A one-time task uses a specific time. Times use the user's timezone.

When a scheduled task is due, the service creates a new task. When it finishes, the agent reports the result in the conversation. While the agent is paused, a due run is skipped. Several runs missed during downtime produce one run. Each user can have 20 scheduled tasks. The scheduled-task board stays visible below the task list, including when empty. Click a title or settings button to edit the name, instructions, recurring rule or one-time date, timezone and enabled state. Common time presets are available; existing custom rules are preserved. Text-only edits do not move the next occurrence. Schedule changes preserve the ID and serialize with firing. Stale editor versions are rejected. Pausing prevents future triggers; changing a schedule does not cancel work already started. Deletion requires confirmation. The agent can also list and update schedules from the main conversation. User-driven follow-ups to scheduled tasks receive a response and are no longer treated as unattended checks.

## Heartbeat

The heartbeat makes the agent start itself at an interval and check the `HEARTBEAT.md` checklist in its workspace. When nothing needs the user, it sends nothing. When something needs the user, it sends one short alert. A scheduled task runs a fixed instruction at a fixed time. A heartbeat item needs judgment, for example "tell me when a task has waited for my approval for more than 2 hours".

1. Open Settings and turn on Heartbeat. It is off by default. The first time, an empty checklist is created.
2. Select Edit list and write one item to check per line. You can also tell the agent "keep an eye on …"; it adds the item to the list.
3. Select the check interval (at least 15 minutes, default 30) and the active hours (default 08:00–22:00; the same start and end means all day). There are no checks outside the active hours.
4. Optional: in Send unprompted messages to, select a connected bot. Heartbeat alerts and scheduled results also go to a direct chat with that bot. Group chats never get them. Without a bot, alerts show on the web only.
5. Select Run once now to check at once. It ignores the active hours and runs at most once a minute.

Rules:

- An empty checklist (headings and comments only) does not run and calls no model.
- A heartbeat waits behind the user's messages. A heartbeat that waited more than 10 minutes is skipped.
- A heartbeat reads the web main conversation. Channel histories remain separate; shared memory supplies information across conversations. For task state it trusts the task list.
- For an item that needs email or a device, a heartbeat starts one background task. When that task finds nothing, the user is not disturbed.
- A heartbeat cannot change, pause or cancel the user's tasks, cannot add or delete scheduled tasks, and never approves an action.
- An alert equal to one sent in the last 24 hours is not sent again. A failed check sends no failure message; the next check runs as usual.
- Each agent runs at most 48 checks a day (configurable). Heartbeats without an alert are removed after 24 hours.

## Connect a Mac, Windows or Linux computer

A local computer can only be connected from the desktop app. The web app has no entry for it, and nothing needs to be downloaded or installed.

1. Run the desktop app in local + cloud dual mode and sign in.
2. In the Personal Agent computer section, click "Connect this computer".
3. Confirm the computer name, optionally tick "Allow control of the screen, mouse and keyboard" and "Allow management of sessions on this computer", then click "Connect". Both start unticked.
4. On macOS, after turning on screen control for the first time, choose "Grant system permissions" in the device menu and allow Screen Recording and Accessibility for the desktop app.

The desktop app's built-in local service keeps the connection: the device is online while the app runs, offline after it quits, and reconnects on the next start. The device credential stays in this computer's private data directory, separated per signed-in account. Switching accounts pauses the previous account's connection; switching back resumes it.

The device polls its own command mailbox. It opens no inbound port. Sent input commands are not delivered again. Computer actions execute one at a time. Other background tasks can run in parallel.

When a Linux desktop runtime has no input driver, the connection offers session management only and screen control shows as unsupported.

## Deploy

Prepare Enterprise Edition, PostgreSQL, Redis, and the OpenSandbox images using the existing deployment guide.

1. Build the computer image: `docker build -f docker/personal-agent/Dockerfile.personal-agent -t hugagent-personal-agent:v5 .`. Do not overwrite a tag that a running computer uses.
2. Set HOST_STORAGE_PATH to an absolute host path.
3. Set PERSONAL_AGENT_OPENSANDBOX_API_KEY to a separate random secret of at least 32 characters.
4. Set PERSONAL_AGENT_OPENSANDBOX_DOMAIN to personal-agent-opensandbox:8080.
5. Set PERSONAL_AGENT_IMAGE to hugagent-personal-agent:v5. Existing computers use the new image when they are recreated.
6. Run the root Compose file with docker/personal-agent/compose.personal-agent.yml and the personal-agent profile.
7. Apply the existing database migration procedure. Apply personalagent01 through personalagent05. These migrations create module tables, backfill message links, create task sessions, and add device session operations.

The extra Compose file creates a separate controller, compute network, and private configuration volume. The controller needs Docker socket access. Do not commit credentials. The existing database encryption key protects sandbox and email credentials.

Missing Personal Agent controller settings cause a failure. The service does not fall back to the ordinary controller. The worker checks permanent computers once a minute. Pausing the task queue preserves the computer.

Wait until the agent has replied to every message before you downgrade personalagent02. The downgrade archives task links and scoped instructions in message metadata. Upgrading again restores them.

## Status panel

The status panel shows the profile, channels, computers, tasks (in progress, then finished), scheduled tasks, and outputs. Select the cloud computer to show its live screen in the canvas on the right. Watching does not interrupt the agent. Select Take control to click, type (input methods and paste work), and scroll on the screen. The agent cannot use the desktop while you control it. Select End control or close the page to release control. Lost connections expire through a lease.

The live screen is a VNC stream. Inside the cloud computer, x11vnc listens only on a private socket, behind a token-checked WebSocket service (port 6090). The backend relays the stream to the browser. Changes are pushed as they happen; a key press reaches the screen in tens of milliseconds. While you watch, the backend drops all keyboard, pointer, and clipboard messages. Only a connection that holds the control lease forwards input. Chinese and other text is sent as Unicode keys, without the clipboard. On macOS, Command acts as Control, so shortcuts such as Command+C work; Command+V pastes the local clipboard.

Computers created before v5 have no live stream; recreate them with the v5 image. The reverse proxy must forward WebSocket upgrades for `/api/v1/personal-agent/computer/stream/ws` (see `src/frontend/default.conf.template`).

Outputs count saved documents, images, and other files. Input attachments are excluded. Select a file name to preview it. Select Download to save it. Older outputs use pagination.

The file preview dialog adapts to the current viewport. Word, PDF, and PowerPoint use the available reading height; Excel, CSV, and long text scroll inside the preview without expanding the page.

The local computer's device menu has two permissions: "Allow screen control" and "Allow session management". Both start off and can only be turned on in the desktop app on that computer; neither the web app nor the cloud can turn them on. Revoking a permission invalidates queued commands.

## Cross-device session management (EE)

Personal Agent manages the user's HugAgentOS projects, chats and runs with one typed tool, `pa_hugagent`. Its operations, arguments and descriptions are generated from the session contract (`hugagent_cli/contract.py`), the same definition the CLI below and the desktop device link use:

- **Conversation turns read only**: list projects and their chats, read chat history and check run status, and answer at once. A turn also sees the user's most recently active projects and the chats the agent recently started, with their run status.
- **Background tasks read and write**: create chats (optionally in a project), send messages, rename, delete, cancel or steer runs. The user approves each write in an approval card; after approval the task's next run picks up the operation's receipt instead of asking again.
- **Run endings are reported**: a task may finish right after sending a message. When the run ends (completed, failed, cancelled or waiting for the user), the result reaches Personal Agent as an event and it tells the user. A run the task itself followed to its end is not reported twice.

The cloud computer includes the lightweight `hugagent` CLI. It manages existing backends.
Web clients and desktop cloud mode share `cloud`. Desktop local mode uses `device:<id>`.

Inside the cloud computer this client is called `hugagent`. Installed anywhere else (a `pip install` of the backend source, or the wheel built by `scripts/build-hugagent-cli.py`), the same client is the `hugagent-session` command with the same options. On a workstation, `hugagent` is the terminal client (`packages/cli`). The local server's `onboard`, `serve`, `doctor` and `engine` commands belong to `hugagent-server`; the session client only points you there when it receives one.

```bash
hugagent device list
hugagent --target cloud chat list
hugagent --target device:<id> chat new --title "Task"
hugagent --target device:<id> chat send <chat_id> "Task instructions"
hugagent --target device:<id> chat history <chat_id>
hugagent --target device:<id> run status <run_id>
```

To start a chat in a project, find the project by name, then create the chat inside it:

```bash
hugagent --target cloud project list --query "Project name"
hugagent --target cloud chat new --project <project_id> --title "Task"
hugagent --target cloud project chats <project_id>
```

A project chat uses the project's instructions and files. Every later message to it, from the web, the desktop or the CLI, runs in its project.

The CLI also supports search, pagination, renaming, pins, favorites, deletion, queued instructions, cancellation, run following, and operation receipts.
Use `--help` for options. Sending a message returns its run ID. Closing the CLI does not cancel the run.
Personal Agent writes use exact-action approval.

Turn on "Allow session management" in the desktop app's device menu to show the monitor's session button. The desktop app manages local sessions as the signed-in account; no local API Key is needed.
Session access and desktop input require separate permissions. Both are off by default.
Revocation stops new operations. Cancel a previously started run separately.

Use a personal API Key. An agent-specific Key cannot manage ordinary sessions.
The cloud computer receives short task credentials, rather than the user's full Key.
Set `PERSONAL_AGENT_CLI_SERVER` to a reachable backend address. Its default is `http://backend:3001`.
The dedicated Compose file connects the backend to the compute network. Single-host deployments need no change.
`PERSONAL_AGENT_CLI_ALLOW_HTTP` defaults to `true`, so the CLI in the cloud computer may reach the backend over plain HTTP on private networks without certificates. Set it to `false` to require HTTPS.

When the cloud-computer sandbox runs on a different server from the application, all three are required:

1. Set `PERSONAL_AGENT_CLI_SERVER` to an application address the sandbox server can reach, such as `http://<application private address>/api` (through nginx). The `backend` service name only resolves inside the same Docker network.
2. Point `PERSONAL_AGENT_OPENSANDBOX_DOMAIN` to the controller on the sandbox server. Commands, files and the live desktop go through the controller proxy, so the application server never connects to sandbox containers directly. Open the controller port only to the application server.
3. Keep the cloud-computer workspace on storage shared by both servers, such as NFS. `PERSONAL_AGENT_VOLUME_ROOT` is the path on the sandbox server and `PERSONAL_AGENT_LOCAL_VOLUME_ROOT` is the path inside the backend container. Both must point to the same data; otherwise persona and memory files written by the backend do not appear in the cloud computer.

The CLI returns JSON receipts. Keep `--request-key` when retrying the same operation.
A request key cannot change its arguments. An unconfirmed dispatched operation reports `outcome_unknown`.
Check the local session before retrying. Devices become unavailable after 20 seconds without a heartbeat.
Operations expire after 60 seconds. Sent operations are never automatically redelivered.

The personalagent05 merge joins the existing Personal Agent and upstream site/browser migration chains. It does not rewrite either chain or change business rows.


## Sidebar task and unread reminders

The Personal Agent entry shows both its task indicator and unread message count. Blue means running, amber requires user input or approval, green means a completed result is unread, and red indicates failure. With concurrent tasks, priority is input required, unseen failure, running, then unread completion.

Reading messages acknowledges only loaded messages actually displayed at the bottom of the conversation; a running blue indicator remains active. After a task completes, a green breathing reminder persists until its result has been delivered and viewed. Viewing failure details clears the red dot; retrying restores the running state. Earlier progress messages cannot acknowledge an undelivered result.

Reminders refresh while navigating to other modules. Hidden browser tabs suspend polling and reconcile on return. Read state is local to this browser, isolated by account and backend, and synchronized between same-browser tabs.

On first use or upgrade, existing messages and completed results are acknowledged once as history; later messages and completions still trigger reminders.

The attention area does not show normal in-flight actions awaiting a receipt. Result-check buttons appear only when the task is waiting and the outcome remains unknown. No execution status bar is added for these actions.

Desktop contention waits automatically without an attention prompt. If a short wait cannot acquire the lock, the task waits durably and checks availability about every three seconds. It requeues after release and acquires the lock again before acting. Manual pauses, pending approvals and unknown effects are not overridden.


The thinking effort selector sits beside the model in settings and defaults to Fast (no extra thinking). It applies to conversation, background tasks, and heartbeat checks; available effort levels follow the model configuration. Each capability section has its own marketplace import button at the top right.

## File memory

Personal Agent uses `USER.md` for stable preferences, `MEMORY.md` for curated facts and decisions, and `memory/YYYY-MM-DD.md` for daily records. It no longer reads or writes the ordinary-chat L1/L2/L3 memory stores or settings. Ordinary chat remains unchanged. Existing memories are not migrated or deleted.

Conversation completion and completed, failed or cancelled tasks durably enqueue a daily record in the same transaction as the outcome. A worker writes the input and result using the user's timezone, redacts recognizable credentials, and retries unavailable workspaces after restart without duplicate entries. Tool output and reasoning are not copied. Earlier history is not backfilled.

Long-term memory is updated through the agent's file tools or the workspace editor and read in subsequent turns. Read existing files before replacing them; concurrent changes require rereading and merging. Daily logs are not automatically promoted wholesale to long-term memory. Settings show the file-memory locations, pending writes and retry status, with a link to view and edit the files.


## Scheduled Dreaming

Memory settings provide a Dreaming toggle, daily hour (03:00 in the agent timezone by default), last/next run and retry status, manual run/retry, and a `DREAMS.md` viewer. Journals are still written after conversation/task completion, not at midnight. Restart coalesces missed schedules into one sweep; backlog continues in bounded batches.

Inspired by OpenClaw Light → REM → Deep, the worker collects current evidence, asks the selected Personal Agent model for a bounded tool-free structured proposal, then validates and writes exact evidence. It does not reproduce OpenClaw recall scoring/query-diversity gates. Reports show sources, reflections, changes, duplicates and conflicting old/new text. Reports are neither injected into normal prompts nor treated as extraction evidence.

Only unchanged platform journal blocks are eligible. Manual notes and edited/deleted blocks are not automatically promoted; changed blocks are reported for manual review. Both MEMORY.md and USER.md promotion require explicit owner memory statements in the web main conversation. Mixed task instructions, quotations, translations and requests not to save remain review-only; task results, assistant output and external-channel speakers stay in journals/reports rather than becoming durable facts. Conservative syntax checks are not a universal semantic safety guarantee. Semantic duplication/conflict classification depends on model judgment; the host enforces evidence, versions and write conditions. Only an unchanged managed entry may be superseded using newer explicit user evidence and the exact prior body. Other conflicts need owner review.

Checkpoints, workspace locks, preimage checks and source identifiers protect replay and concurrent edits. Failures back off for 1, 2, 4 and 8 minutes, up to five automatic attempts, then await manual retry. If disk is unavailable, durable failure status remains available through settings even when a failure report cannot be written. Each collection pass scans at most 100 receipts and reads at most 32 MiB of journals (16 MiB per file), sending at most 24 chunks/48,000 characters to the model. Oversized entries are processed in chunks. Memory files above 256 KiB or reports above 16 MiB stop consolidation visibly and remain intact for manual maintenance. Existing prompt injection budgets still apply. Ordinary-chat memory is unchanged.

Daily journal writes are capped at 16 MiB per file. Oversized files preserve pending receipts and retry, but require owner archival before delivery can resume. Receipts for deleted conversations are skipped so they cannot block other conversations.

## Source and edition boundaries

The shared backend lives in `src/backend/personal_agent/`, organized by identity, conversations, tasks, scheduling, workspace, computers, sessions, channels and delivery. The frontend follows the same feature boundaries. Both editions share execution logic; EE adds its account access policy.

CE uses its independent `ce_0028` migration for the seven Personal Agent tables. Existing data is preserved and downgrade refuses to drop populated tables. The full repository retains its original migration history. The module is off by default; existing authenticated CE users can use it once enabled.

### Steering a running task

Additional user messages enter context automatically at the next model or tool boundary. Unstarted tool calls yield to replanning; tools already running retain their results. No status-tool call is required to resume execution. Tasks do not use input version counters. Cancellation, execution ownership, leases and approvals still apply. Input arriving after the last reasoning boundary continues in a subsequent execution rather than being lost.
