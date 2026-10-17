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
operations reuse core/services/session_operations; neither path calls HTTP route
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
