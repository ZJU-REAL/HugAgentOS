# Personal Agent

Shared optional module for CE and EE. Enable with PERSONAL_AGENT_ENABLED=true.
Ownership checks and action approvals apply in both editions. CE admits existing
authenticated users; EE additionally applies its account capability policy.

## Responsibilities

- api: HTTP validation, authentication dependencies and route composition.
- identity: identity creation, settings and available capabilities.
- conversation: foreground replies, conversation isolation and message routing.
- tasks: work items, approvals, execution, leases and task state transitions.
- scheduling: schedules and heartbeat admission.
- workspace: persona files, prompt assembly, file memory and dreaming.
- computer: cloud sandbox, devices, live screens and human control.
- sessions: the typed session tool, CLI authorization, operation queue and watches.
- channels: inbound routing and channel delivery orchestration.
- delivery: output resources, file publication and delivery receipts.
- tools: model-facing tool adapters.
- context.py: request-scoped AgentContext composed from identity, conversation and tasks.
- background.py: lifecycle of the periodic loops, separate from task leases.
- models.py: the seven shared tables, registered even with the runtime module off.

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
