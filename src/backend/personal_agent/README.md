# Personal Agent

Shared optional module for CE and EE. Enable with PERSONAL_AGENT_ENABLED=true.
Ownership checks and action approvals apply in both editions. CE admits existing
authenticated users; EE additionally applies its account capability policy.

## Responsibilities

- api: HTTP validation, authentication dependencies and route composition.
- identity: identity creation, settings and available capabilities.
- conversation: foreground replies, conversation isolation and message routing.
  attention.py: failed messages and undelivered replies for the page, with their retries,
  and finding a message or task by an IM link, message ID or text.
  router/: the agent's own turn. Producers enqueue Triggers (kinds.py says what each kind
  means to a turn); the scheduler claims them into a Turn that holds the lease; turn.py runs
  and settles it; context.py builds the prompt and history.
- tasks: work items, approvals, execution, leases and task state transitions.
- scheduling: schedules and heartbeat admission.
- workspace: persona files, prompt assembly and the daily journal writer (journal/).
- memory: the topic memory wiki: page rules and lint, the single write path, the write
  log, snapshots, per-turn injection, the one-off migration and Dreaming. See
  memory/README.md.
- computer: cloud sandbox, devices, live screens and human control.
- sessions: the typed session tool, CLI authorization, operation queue and watches.
- channels: inbound routing and channel delivery orchestration. im_actions.py: the
  acknowledgement placeholder (edited into the reply or recalled), IM replies that quote
  a task's message going straight to that task, and approvals and task questions answered in IM.
- delivery: output resources, file publication and delivery receipts.
- tools: model-facing tool adapters.
- context.py: request-scoped AgentContext composed from identity, conversation and tasks.
- background.py: lifecycle of the periodic loops, separate from task leases.
- models.py: the shared tables, registered even with the runtime module off.

HTTP routers call the domain services. Model tools use the same services. Session
operations reuse core/chat/operations; neither path calls HTTP route
functions. Shared chat request contracts live in core/chat/schemas.py. External
integration uses the registered hooks in extension.py.

Do not add edition checks to execution or tools. Keep the edition policy in
access.py and edition-owned adapters. Do not import edition_ee from this module.

## Database and derivation

Table names and existing full-edition migration history are unchanged. CE uses
its own ce_0028 migration for all seven tables; it is safe after a fresh metadata
bootstrap and refuses downgrade while Personal Agent data exists.

The CE manifest retains this module, its frontend, prompts, desktop image,
Compose profile and tests. CE overlay files must preserve model exports,
authentication availability, navigation and dictionary assembly. Validate the
physically derived CE tree as well as the full tree; setting JX_EDITION=ce inside
the full checkout is not sufficient.

## Memory upgrade

The first seeding after the upgrade runs memory/migration.py once per agent: daily
journals move from memory/YYYY-MM-DD.md to journal/, MEMORY.md keeps the old Dreaming
quotes under `## 置顶` and its other text moves to journal/legacy-memory.md, and the next
Dreaming run replays up to 90 days of history to build the first wiki. The marker is
`PersonalAgent.config.memory_layout = 2`. Without a workspace volume the step is skipped
and retried on a later seeding. No ordinary-chat memory is touched.

## IM allowed list

A bot routed to Personal Agent acts as its owner, so only the owner's IM accounts may
instruct it (channels/owner_binding.py). The owner keeps an allowed list per bot in the
web settings (接入频道): pick an account from the ones that recently messaged the bot
privately, or type its ID. Nothing a message says adds an account. State lives in
`PersonalAgent.config`: `channel_allowed[channel_id]` (a list of `sender_id`,
`sender_name`, `keys`, `added_at`) and `channel_recent_senders[channel_id]` (the last ten
private senders not listed: ID, name, keys, time; never the text).

- Nobody listed: a private message gets a short "not on the list" reply; no conversation,
  message, inbox row or task is created. Group messages are kept as context only.
- Listed accounts' private and group messages reach the agent and may answer approvals.
  Anyone else's private message is dropped without a reply; their group messages are
  context only.
- Alerts and scheduled results go only to a private conversation a currently listed
  account wrote in after it was listed (`channel_origin.sender_keys`).
- `POST /channels/{id}/allowed` (`key` of a recent sender, or `sender_id`) and
  `DELETE /channels/{id}/allowed?key=`, for the Personal Agent's own authenticated user
  and only for their own bots. Display names can be changed by anyone; settings show the
  ID and time next to each recent sender.

Upgrade rule: an account bound with a pairing code (`channel_owners[channel_id]` with
`bound_at`) counts as listed until the list is first edited. Owners learned before pairing
existed (no `bound_at`) are not trusted.

A typed DingTalk ID is matched as a union or encrypted ID only, never as a userid, which
is only unique together with its organisation; pick DingTalk accounts from recent senders.
Matching uses `keys`, scoped identities computed when pairing. Most platforms give one
stable sender ID per bot (`id:<sender_id>`). DingTalk gives up to three: `senderUnionId`
(`union:`), `senderStaffId` with `senderCorpId` (`staff:<corp>:<userid>`; the userid is
only unique inside an organisation, and DingTalk leaves it empty for org-external users
and for bots not yet published), and the encrypted `senderId` (`sid:`). A message matches
when any key is shared. DingTalk does not document whether `senderId` is the same in
private and group chats for an external user; if neither the union ID nor the encrypted
ID carries over, the owner is treated as someone else in that group (fails closed) and
has to give instructions privately.

## Task transcript ordering

Task details use the private conversation's monotonic `chat_seq`. When a user
instruction arrives during a run, subsequent text, thinking and tool calls start
a new assistant message after that instruction. Buffered earlier output stays in
the earlier message. Results of tools started before the instruction update their
original tool cards. Split messages retain the run ID so completion reminders can
identify the latest response. This affects new execution output; existing mixed
transcripts cannot be reconstructed without their original event boundaries.

## Task steering

TaskSteering reads actual linked user messages and shares the ordinary
SteerMiddleware boundaries. Delivery acknowledgement records message IDs only
after insertion into model context. Starting tool batches yield to new input;
in-flight tool results remain valid. Completion checks for unread messages under
the task lock and queues another execution if input arrived too late.

Task input revisions and session-operation task revisions are removed by
personalsteer01 (full) and ce_0029 (CE). Stop task workers before upgrading; the old
worker code expects these columns. Downgrade recreates obsolete counters at zero,
not their historical values. It must also run with workers stopped. No user
messages, task results or files are removed. File delivery request keys now stay
unique within a task, including continuations: use a new key for a new output.
