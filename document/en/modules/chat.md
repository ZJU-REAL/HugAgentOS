# Chat & Agent Orchestration

> Last updated: August 26, 2026

Chat is the core pipeline of HugAgentOS: a user message travels through the FastAPI route, runtime-context assembly, and the streaming orchestrator, then an AgentScope 2.0 ReActAgent drives multi-turn "think → call tool → observe" loops whose events are pushed to the frontend in real time over SSE. This page walks the end-to-end flow as it exists in the code, then covers the citation system, plan mode, sub-agents, conversation summarization, chat sharing, context compression, and oversized-tool-result offloading.

> All orchestration code lives in `src/backend/orchestration/` (the legacy `routing/` package has been fully migrated there).

## End-to-end flow of one conversation

```
Browser ── POST /v1/chats/stream ──▶ api/routes/v1/chats.py::chat_stream
   │   1. _ensure_main_model_configured()   503 immediately if no main model
   │   2. auth / chat-ownership checks / read user capabilities & memory flags
   │   3. core/chat/context.py::build_runtime_context()  assemble workflow context
   ▼
orchestration/chat_run_executor.py::start_run()
   │   creates a ChatRun row + spawns a background asyncio.Task (decoupled from HTTP)
   │   every chunk becomes an SSE event XADD'ed to Redis Stream jx:chat:run:{run_id}:events
   ▼
orchestration/workflow.py::astream_chat_workflow()
   │   ├─ orchestration/memory_integration.py  non-blocking memory retrieval (bg task + budget)
   │   ├─ core/config/catalog_resolver.py      resolve enabled skills/mcp/kb for this request
   │   ├─ core/llm/agent_factory.py::create_agent_executor()
   │   │     MCP pool + skill registration + file tools + system prompt + middlewares → Agent
   │   ├─ core/llm/context_manager.py          trim history to the token budget
   │   └─ orchestration/streaming.py::StreamingAgent.stream()
   │         consumes agent.reply_stream(), coalesces 25 event kinds while preserving tool-argument deltas
   ▼
SSE follower: chat_run_executor.follow_run_as_sse()
       XRANGE replay + XREAD tail → data: {...}\n\n → browser
       (frontend parsing in src/frontend/src/hooks/useStreaming.ts + App.tsx)
```

### Run decoupling and reconnect/resume

Every sent message creates a `ChatRun` and a background task (`orchestration/chat_run_executor.py`); events are written to a Redis Stream (`maxlen=5000`; the default TTL is the 7,200-second maximum human wait plus a 1,800-second recovery grace period). The HTTP connection is merely a *follower*, which enables:

| Capability | Endpoint |
|---|---|
| Start a streaming chat | `POST /v1/chats/stream` |
| Resume after refresh / disconnect | `GET /v1/chats/stream/{run_id}?from_offset=N` |
| Probe for an in-flight run | `GET /v1/chats/{chat_id}/active-run` |
| Cancel a run (kills the background task) | `POST /v1/chat-runs/{run_id}/cancel` |
| Add an instruction at the next safe ReAct boundary | `POST /v1/chat-runs/{run_id}/steer` |
| Query durable queued-input state | `GET /v1/chat-runs/{run_id}/steers` |
| Withdraw an instruction that hasn't taken effect | `DELETE /v1/chat-runs/{run_id}/steer/{steer_id}` |

Defensive machinery: a `: heartbeat` SSE comment line every 15 silent seconds (keeps nginx `proxy_read_timeout` and other proxies from cutting the stream); an inactivity watchdog fails the run if the workflow produces no meaningful output for 600 s (`CHAT_RUN_INACTIVITY_TIMEOUT_SEC`); internal heartbeats keep that watchdog alive only while the backend registry contains a real pending human interaction; a periodic reaper collects over-age and quiet running runs; `recover_orphan_runs()` cleans up leftovers at startup.

### Model-initiated user questions

Regular top-level chats register `ask_user_question`. The model may use it only for a user preference, authorization decision, or material ambiguity that context and available tools cannot resolve, and should consolidate one decision point into one concise round. Turbo, plan execution, automation, sub-agent, and other unattended/non-top-level paths do not register it, so background work cannot wait indefinitely for a UI.

The model-facing contract matches deepseek-harness: `questions[]` contains `id`, `question`, optional `header`, optional label/description `options`, and `multi_select`. Options have no model-visible ID or `recommended` field; a recommended option goes first and appends `(Recommended)` to its label. A successful result is the compact `{"answers":[{"id":"scope","selected":["Current page (Recommended)"],"custom":"..."}]}` shape, where `selected` contains the original option labels. Private option IDs used by the browser answer API are transport details and never enter the model's tool schema or result.

Calling the tool suspends the current ReAct tool coroutine in place: it does not end the ChatRun or create a synthetic “continue” turn. On `user_question`, the frontend replaces the normal composer with a resident question composer supporting single-select, multi-select, recommended choices, custom text, paging, skip, and cancel. Answers use out-of-band endpoints. A successful POST, `user_question_resolved` SSE, or authoritative pending snapshot removes only the exact `request_id`, so duplicate clicks, concurrent browser tabs, and late responses are settled by the server's first-valid-claimant rule.

After refresh, disconnect, or chat switching, pending endpoints restore only requests that still exist in the backend registry, and the sidebar marks those chats as **Waiting for your answer**. `HUMAN_INTERACTION_MAX_WAIT_SECONDS=7200` controls the default maximum wait. Timeout, cancellation, or unavailable interaction is surfaced as a structured tool error (`ASK_TIMEOUT`, `ASK_CANCELLED`, or `NO_PROVIDER`); the system policy tells the model not to repeat the same question and to use a safe default when appropriate. Like the existing write-confirmation gate, the registry is currently process-local; a service restart fails the original ChatRun, and the client does not advertise the stale question as resumable.

### Mid-run follow-ups, Steer, and the stop shortcut

While a regular chat is generating, the composer continues accepting the next
message. Sending it creates a queued card above the composer. You can edit the
card from its more menu or delete it. If you don't select **Steer**, the client
sends the message as the next turn after the current answer finishes.

The home-page and in-conversation composers grow with multiline content, then scroll internally after reaching their visible height limit. Use `Shift+Enter` to insert a line break.

Queued input is database-authoritative: acceptance happens before Redis sends a
best-effort wake-up. Each chat gets a monotonic `steer_seq`; rows move through
`accepted`, `claimed`, `applied`, `cancelled`, or `superseded`, and expired
claim leases are redelivered. `delivery_mode=steer` injects the instruction at
the next safe ReAct boundary and confirms it with `steer_applied`.
`delivery_mode=followUp` starts after the current run completes, while
`delivery_mode=nextRun` becomes the next independent run without modifying the
current context. Both handoff modes atomically commit the current answer, queued
user message, queue state, and next `ChatRun`. The status endpoint above lets
the client reconcile after refresh. Messages containing attachments, skills,
connectors, plugins, or sub-agents still wait and send normally. Pressing `Esc` cancels the
run for the chat visible on the current page.

### Agent construction highlights (core/llm/agent_factory.py)

`create_agent_executor()` is the shared factory for every mode (main chat, plan, batch, sub-agents, automation):

- **MCP tools**: after the three-layer filter of catalog + per-user overrides + request context (see [Capability Center](catalog.md)), stable servers reuse the process-level connection pool (`core/llm/mcp_pool.py`); per-request servers (e.g. `retrieve_dataset_content`, which needs per-request HTTP headers) are spawned fresh; the user's self-added private MCP servers are merged in with owner isolation.
- **Skills**: registered as AgentScope Agent Skills via `core/agent_skills/loader.py`, with `view_text_file` allow-listed to read SKILL.md files (see [Agent Skills](agent-skills.md)).
- **File / sandbox tools**: `bash`, `sandbox_put_artifact`, `sandbox_get_artifact` are always registered; Read/Edit/Write/Glob/Grep/Delete/Move/mkdir plus the MySpace tools are gated by `CODE_CAPABILITY_ENABLED` and share one `ReadStateTracker` to keep the "must Read before Edit" invariant.
- **Middlewares** (onion model, `core/llm/middlewares.py`): `DynamicModelMiddleware` (switches the model per chat_mode, see [Model Providers](model-providers.md)), `FileContextMiddleware` (injects uploaded/historical file context), `SteerMiddleware` (injects follow-ups after tool results and before the next reasoning round), `WorkspacePinHintMiddleware`, `FinishPinGuardMiddleware`.
- **Context compression**: `CompactingAgent` routes the pre-turn, mid-turn, and post-turn trigger points through one persistent checkpoint engine and one Codex-style handoff prompt. `ContextConfig` remains only for tool-result limiting and AgentScope overflow fallback, and that fallback reuses the same prompt. If its structured call still fails, the model adapter returns an L3 synthetic summary so the active reply is not aborted by a compression error.
- **Permissions**: every registered tool gets a native `PermissionRule(ALLOW)` seed, preserving AgentScope's built-in dangerous-operation checks (no blanket BYPASS).
- **Iteration caps**: main agent defaults to `max_iters=50`, isolated sub-agents to 10.

## SSE event types and payloads

`orchestration/streaming.py::StreamingAgent` coalesces AgentScope 2.0 `reply_stream` events into internal events. Tiny tool-argument fragments are batched at 256 characters or 50ms, keeping them visible without recreating an SSE event storm. `workflow.py` and `chats.py::_stream_sse_response` enrich them with chat-level fields before they hit the wire. Events as the frontend sees them:

| `type` | Meaning | Key fields |
|---|---|---|
| `thinking` | Reasoning (delta or stage hint) | `delta` / `message` |
| `content` | Answer text delta | `event: "ai_message"`, `delta`, `chat_id` |
| `content_replace` | Replaces the streamed draft in place when ontology review revises the final answer | `content`, `reason: "ontology_review"`, `chat_id` |
| `tool_call_start` | Tool-call construction starts; the frontend opens one card by stable ID | `tool_name`, `tool_display_name`, `tool_id` |
| `tool_call_delta` | Incremental argument JSON appended to the same card | `tool_name`, `tool_id`, `arguments_delta` |
| `tool_call` | Arguments are complete and execution is about to start | `tool_name`, `tool_display_name`, `tool_args`, `tool_id`, plus `subagent_name` for sub-agent calls |
| `tool_result` | Tool invocation result | `tool_name`, `result`, `tool_id`, `status`, `citations[]` |
| `steer_applied` | A mid-run instruction entered the ReAct context | `steer_id`, `message`, `message_id`, `chat_id` |
| `queued_run_started` | A transactionally committed `followUp` / `nextRun` child starts and the frontend follows it immediately | `run_id`, `message_id`, `user_message_id`, `message`, `queue_id`, `steer_id`, `delivery_mode` |
| `subagent_event` | Child execution details nested under the parent `call_subagent` card | `parent_tool_id`, `sub_type`, `agent_name`, plus child tool or content fields |
| `ontology_activation` / `ontology_gate` / `ontology_review` | Ontology-governance state, separate from model reasoning | workflow activation, gate decision, and committee status or verdict |
| `tool_pending` | Waiting fallback when the provider exposes no parseable argument deltas | `reason` |
| `batch_confirm` | Batch plan generated, awaiting user confirmation (human gate) | `plan_id`, `total`, `preview`, `default_template`, `placeholder_keys` |
| `file_confirm` | A tool is suspended awaiting confirmation of a MySpace write | confirmation context; the tool resumes in place after an out-of-band `POST /v1/chats/{chat_id}/file-confirm` |
| `user_question` | `ask_user_question` is suspended awaiting the chat owner's answer | `request_id`, `questions[]`, `created_at`, `expires_at`, `chat_id` |
| `user_question_resolved` | The server settled an answer, cancellation, or timeout | `request_id`, `outcome`, `chat_id` |
| `context_usage` | Context snapshot for the latest primary-model call; the total is measured when the provider returns usage | `source`, `exact`, `prompt_tokens`, `completion_tokens`, `used_tokens`, `context_window`, `breakdown` |
| `compaction_notice` | A new context-compaction checkpoint was created after the previous turn | `chat_id`, `context_compaction` (coverage boundary and replacement-summary token count) |
| `meta` | End-of-turn metadata | `route`, `citations[]`, `sources`, `artifacts`, `workspace_files`, `ontology_governance`, `warnings`, `is_markdown`, `message_id`, `usage`, `context_usage`, `compaction_pending` |
| `error` | Failure (mapped to a user-friendly message) | `error`, `chat_id` |
| `heartbeat` | Heartbeat (event-level; a `: heartbeat` comment line also exists) | — |

The stream terminates with `data: [DONE]`. Example frames:

```
data: {"type":"tool_call_start","tool_name":"internet_search","tool_display_name":"Web Search","tool_id":"call_abc"}

data: {"type":"tool_call_delta","tool_name":"internet_search","arguments_delta":"{\"query\":\"Beijing ","tool_id":"call_abc"}

data: {"type":"tool_call_delta","tool_name":"internet_search","arguments_delta":"IC industry\"}","tool_id":"call_abc"}

data: {"type":"tool_call","tool_name":"internet_search","tool_display_name":"Web Search","tool_args":{"query":"Beijing IC industry"},"tool_id":"call_abc"}

data: {"type":"tool_result","tool_name":"internet_search","result":{...},"tool_id":"call_abc","citations":[{"id":"e1","title":"...","url":"...","snippet":"...","source_type":"internet","item_index":0}]}

data: {"type":"content","event":"ai_message","delta":"Based on the search results…","chat_id":"chat_x"}

data: {"type":"context_usage","source":"provider","exact":true,"prompt_tokens":1234,"completion_tokens":456,"used_tokens":1690,"context_window":128000,"breakdown":{...}}

data: {"type":"meta","route":"main","citations":[...],"usage":{"prompt_tokens":3700,"completion_tokens":900,"total_tokens":4600,"llm_call_count":3},"context_usage":{"source":"provider","exact":true,"prompt_tokens":1234,"completion_tokens":456,"used_tokens":1690,...},"message_id":"msg_..."}

data: [DONE]
```

After `meta`, `chat_run_executor.py` persists the assistant message, backfills artifacts,
and launches a background follow-up-question generator
(`orchestration/followups.py`; results land in the message's
`extra_data.follow_up_questions` and are fetched through
`GET /v1/chats/{chat_id}/messages/{message_id}/followups`). The frontend
collects ontology events in a standalone **Domain Ontology Governance** module
instead of model reasoning. The model draft continues to stream token by token.
If the committee changes the answer, the backend sends one `content_replace`
event, the frontend replaces the body in place, and the database stores only
the reviewed final answer. It persists `ontology_governance` with the assistant
message so the module remains available after a history refresh.

`usage` is cumulative billing data across all model calls in the turn.
The context gauge instead consumes `context_usage`: `prompt_tokens +
completion_tokens` for the latest primary-model call. It never divides
cumulative ReAct billing by the context window. When the provider returns
usage, the `source=provider` headline is authoritative; category values come
from the final backend request manifest and are reconciled to that measured
total. Only providers without usage fall back to `backend_estimate`. The
frontend excludes display-only sub-agent `subSteps`, historical reasoning,
and fixed system reserves. An unsent draft or staged file has not reached a
model yet, so it is added only as an explicitly labelled local estimate. The
snapshot is persisted with the assistant message and is also available from
`GET /v1/chats/{chat_id}/context-usage`.

History replay recognizes both reasoning protocols. Inline-reasoning models may
emit `reasoning</think>body`, while the backend normalizes a structured reasoning
field to `<think>reasoning</think>body`. The frontend places reasoning and tool
calls in one process area and renders all visible body text as one continuous
Markdown block. History rendering doesn't split the body at character offsets.

## Citation system (Evidence Anchors)

Citations make every fact in the answer traceable back to a specific tool result. Numbering authority belongs to a single backend source of truth — the model only **copies** ids, never computes them. The chain has four segments:

1. **Anchor allocation & injection (backend middleware)**: `core/llm/middlewares.py::CitationAnchorMiddleware` hooks AgentScope 2.0's `on_acting` and, before a tool result reaches the model, calls `orchestration/citation_anchor.py` to extract → allocate → inject: each citable item gets a session-monotonic anchor id (`e1`, `e2`, … — unique across tools, calls, and turns; a new turn continues from the max anchor found in the chat's persisted messages), and `"cite_id": "e7"` is written into the result JSON in place (plain-text results get a trailing `[cite_id: e7]` line). **The allocator is bound to the agent instance** (`attach_allocator()` / `resolve_allocator()`), which is how the orchestrator and the middleware share one counter; the ContextVar is only a fallback for sub-agent chains, because `astream_chat_workflow` is an async generator whose context does not reach the task the agent actually runs in. Extraction degrades through four layers: tool-declared `__citations__` → the tool spec registry (`TOOL_SPECS` config: list paths + CN/EN field aliases) → a generic heuristic (unique dict-array field) → the whole result as one anchor. Operational tools (file writes, pin, etc. — `SKIP_TOOLS`) pass through untouched. Any exception passes the original result through — citations degrade, the conversation never breaks.
2. **Prompt contract**: the system prompt (fallback file `prompts/prompt_text/default/system/40_format.system.md`; the active DB version is authoritative at runtime) needs only one tool-count-independent rule: copy the `cite_id` annotated in the result verbatim into `[anchor text](cite:e7)` (or `[来源](cite:e7)` at sentence end); never self-number.
3. **Orchestration consumption**: each `tool_result` event calls `collect_citation_dicts()`, which fetches `CitationItem`s (`id` / `tool_name` / `tool_id` / `title` / `url` / `snippet` / `source_type` / `item_index`) from the allocator registry keyed by `tool_id`; when no allocator is installed (legacy replay paths) it falls back to the old offset extraction in `orchestration/citations.py`. `source_type` values: CE ships `internet`, `knowledge_base`, and `database`; industry citation types such as `industry_news`, `ai_news`, `chain_info`, and `company_profile` are added by the Enterprise Edition (EE).
4. **Frontend rendering**: citations ride on `tool_result` and `meta` events and are persisted with the message (what is persisted is the annotated result, so replay/share shows the same numbering as generation). `components/citation/CitationMarkdownBlock.tsx` recognizes three marker forms in parallel — `[anchor text](cite:eN)` (rendered as a text link with a hover source card), `[[eN]]` (obsidian-style tolerance), and the legacy `[ref:tool_name-N]` (historical messages, rendered as a superscript badge). Tool cards show a matching `cite_id` chip on each item (`jx-tr-citeTag`), and tools without a dedicated renderer fall back to a generic list-card renderer.

**Tool development convention**: a tool (in-house or MCP) that wants precise citation granularity should return a `__citations__` field in its JSON — `[{"title": "...", "url": "...", "snippet": "...", "source_type": "..."}, …]`, entries ordered to match the result body; the middleware adopts it verbatim and injects `cite_id` in place. Tools without the field fall back to registry config or heuristics — at worst the whole result becomes one anchor, so **every tool is citable by default**. See the citation-declaration section in [MCP tools](mcp-tools.md).

## Plan Mode

Plan Mode splits complex tasks into "generate plan → user reviews/edits → execute step by step", implemented in `orchestration/subagents/plan_mode.py`:

- **Generate** (`astream_generate_plan` / `POST /v1/plans/generate`): a "bare LLM" agent (`disable_tools=True`) produces a structured JSON plan. System-prompt resolution: active `plan_mode` version in the prompt pool → legacy `system/90_plan_mode` part → fallback file `prompts/prompt_text/plan_mode/plan_mode.system.md` → hardcoded minimal prompt.
- **Execute** (`astream_execute_plan` / `POST /v1/plans/{plan_id}/execute`): each step gets its own agent, executed sequentially, with step-level MCP/skill/sub-agent bindings and cancellation (`is_run_cancelled` polling); execution also goes through ChatRun + Redis Stream, so it survives disconnects.
- **Frontend presentation and title**: manual Plan Mode keeps plan previews and execution progress in the in-conversation plan card instead of duplicating them in the compact strip above the composer (that strip is reserved for model-driven `update_plan` progress in regular chats). A model-generated conversation title is requested as soon as the preview is ready, with a first-task title used as the temporary fallback.
- **History is separate from the active mode**: the chat's `planChat` marker and `plan_snapshot` only preserve its sidebar classification and historical plan cards; an independent per-chat composer state decides whether the next message uses Plan Mode. Turning Plan Mode off keeps prior plans and reports visible, while later requests (for example, generating a presentation from a report) run as ordinary chat and stay that way after refresh or re-entry.
- **Model role**: plan mode prefers the `plan_agent` role and falls back to `main_agent` (the `_mode_role` branch in `agent_factory.py`).
- Unattended modes (plan execution / automation) remove `batch_runner` from the toolkit, since `batch_plan`'s confirmation dialog has no UI in those contexts (`workflow.py::_resolve_batch_runner_visibility`).

## Sub-agents

The regular main-chat harness always provides three platform defaults. They
are not `UserAgent` rows, use reserved IDs, and cannot be shadowed by a
user-created agent:

| ID | Role | Conversation context | Project workspace | Capability boundary |
|---|---|---|---|---|
| `builtin.explorer` | Explorer | Independent brief; no parent history | Shared, read-only | No Bash; only the intersection of parent-enabled query MCPs |
| `builtin.worker` | Worker | Full parent conversation history | Shared, writable | Inherits this run's parent skills, MCPs, and KBs; grants nothing new |
| `builtin.reviewer` | Reviewer | Independent brief; no producer history | Shared, read-only | No Bash; independently verifies and returns `pass / revise / escalate` |

All three appear directly in the user-facing **Sub-agents** page with a
**Built-in** badge and start enabled for every user. Each user can toggle them
independently; the disabled set is persisted in
`users_shadow.metadata.disabled_builtin_subagent_ids`, so it follows the
account across browsers. A disabled role remains in the library so it can be
re-enabled, but is removed from `@` candidates, explicit-language delegation,
and autonomous routing, and cannot start a dedicated conversation. Its prompt
is shown read-only on the detail page instead of as an editable Config prompt
tab. The detail page's **Capability Policy** shows that these roles load the
main agent's effective capabilities dynamically at runtime and makes the
read-only narrowing for Explorer and Reviewer explicit, instead of presenting
dynamic inheritance as “not bound.”

Context and workspace sharing are separate dimensions: Explorer and Reviewer
can inspect current files without being anchored by the parent conversation or
implementation trace, while Worker needs the complete user constraints and
prior decisions. The main agent's dynamic routing table lists only capabilities
that are enabled for the current run and permitted by the role policy. A skill,
MCP server, knowledge base, or native tool that the parent disabled, lacks
permission to use, or lost during runtime filtering is neither advertised nor
delegated to a platform default. None of the three may delegate further. There
is deliberately no built-in planner because the regular main agent already
maintains plans via `update_plan`, avoiding “plans inside plans.” At runtime,
their prompts remain the independent `explorer / worker / reviewer` parts of
the `subagents` prompt-pool kind. That kind stays in backend storage and
migration snapshots for compatibility;
`prompts/prompt_text/subagents/` is only the seed and failure fallback.

In addition, user-created sub-agents (`api/routes/v1/agents.py`, DB table `UserAgent`)
can carry their own system prompt, MCP, skill, plugin, and knowledge base
bindings, plus model parameters such as provider, temperature, `max_tokens`,
and `max_iters`. When you create or edit a sub-agent, the resource picker
supports these sources:

- Installed skills and plugins.
- The skill and plugin marketplaces. After installation, the resource is bound
  to the current sub-agent automatically. Resources that require credentials
  still use the existing credential form and installation permission checks.
- MCPs that you have personally disabled but an administrator still permits.
  This explicit binding applies only to the current sub-agent and doesn't
  enable the MCP for the main agent. An administrator-disabled MCP remains
  unavailable.

User-created sub-agents support four access paths. Platform defaults are
reached through autonomous main-agent dispatch or explicit natural-language
delegation. Orchestration ownership differs by path:

The composer provides two keyboard launchers. Typing `@` first shows the
currently available shortcuts for files, sub-agents, plan mode, batch
execution, workflow mode, and autonomous loops; continued typing searches
sub-agents directly, while choosing **Sub-agents** opens the complete picker.
Typing `/` groups commands and descriptions by plugin and skill; conversation
modes are selected through `@`. Both pickers support arrow-key navigation,
Enter or Tab to confirm, and Escape to go back or close. Unavailable or unauthorized capabilities are omitted.

The `+` menu at the lower-left of the composer also lets users select enabled
sub-agents, skills, connectors, and plugins directly. Selecting a connector
adds an `MCP` chip and explicitly activates that connector for the current turn
only; after sending, the conversation history keeps the connector as a badge.
An explicit connector selection is a mandatory invocation: the first model
round is restricted to tools exposed by that connector and must complete at
least one real tool call before the answer can continue. If the connector
cannot connect, exposes no callable tools, or the model provider ignores the
required-call constraint, the turn fails explicitly instead of silently
answering without the connector.

- **Structured `@` delegation**: selecting one `@sub-agent` in the composer
  sends both `mention_agent_id` and its display name. The backend removes the
  display-only `@name` prefix and injects a strict per-turn delegation
  constraint. The main model keeps its normal reasoning and token stream, and
  its next genuine tool call must be `call_subagent` for the selected target;
  it cannot query data first. The complete child execution happens inside that
  tool, with reasoning, tools, and text emitted as `subagent_event` entries
  under the real tool card. The main model then streams the integrated answer.
  The turn stays on the `main` route and does not permanently bind the regular
  chat to that sub-agent. Older clients that send only `mention_name` are
  accepted only when exactly one accessible agent has that name.
- **Explicit natural-language delegation**: a Chinese command that starts with
  `调用` or `请调用`, contains one unique and complete accessible sub-agent
  name, and ends with an action-oriented task resolves the target and injects a
  constraint into the current user turn. The backend doesn't fabricate tool
  events or bypass the main model. The main model keeps its normal reasoning
  and streaming path, and its next real tool call must be `call_subagent` for
  the resolved target; it can't call another tool first. For example,
  `调用企业风险分析子智能体 分析杭州量知的风险` displays the `call_subagent` card when
  the model issues the real call. Child reasoning and tools arrive as
  `subagent_event` entries under that card, and the main model then streams its
  integrated final answer. The turn keeps the `main` route, while
  `call_subagent` and child tools retain their real audit logs. Ambiguous names,
  disabled targets, empty tasks, and discussion questions don't trigger forced
  delegation.
- **Dedicated conversation**: a chat opened from the sub-agent detail page uses
  `agent_id`, so subsequent turns continue with that sub-agent.
- **Autonomous main-agent dispatch**: when neither a structured `@` selection
  nor the strict natural-language command matches, the main agent can use the
  `call_subagent` tool registered by
  `core/llm/subagent_tool.py`. Each child runs in its own thread and event loop,
  then returns text for the main agent to integrate. This path supports parallel
  sub-agents, task decomposition, and cross-domain synthesis.

## Conversation summarization & context compression

Three complementary layers:

| Layer | Implementation | Trigger |
|---|---|---|
| Chat title summary | `core/llm/summarizer.py::ConversationSummarizer` (`summarizer` model role, `ENABLE_SUMMARY` flag), `POST /v1/summary` | Auto-titling new chats |
| Unified context checkpoint | `core/services/compaction_service.py::run_compaction()`; pre-turn, mid-turn, and post-turn share one trigger ratio, handoff prompt, replacement shape, and persistent checkpoint | Context reaches the configured fraction of the model window |
| Deterministic overflow protection | `ContextConfig.tool_result_limit` bounds individual tool results first; when unified compaction fails or persistence is disabled, the AgentScope fallback compacts the live in-memory context with the same handoff prompt | A tool result is oversized or unified compaction is temporarily unavailable |

Compaction checkpoints are internal `system` messages; they do not hide or
delete any user-visible transcript entries. A checkpoint also stores the
backend's post-compaction estimate for the final system prompt, tool schema,
replacement history, and provider framing. When end-of-turn background
compaction starts, `meta.compaction_pending` makes the frontend poll
`GET /v1/chats/{chat_id}/context-usage` within the bounded summarizer window.
Once the new checkpoint commits, the gauge immediately switches to
`compaction_estimate` instead of waiting for another turn or page refresh.
The next primary model call replaces that estimate with fresh upstream usage.
Message-list responses and the next turn's `compaction_notice` continue to
return the same checkpoint boundary so refresh, stream replay, and cross-tab
recovery stay consistent.

## Oversized tool-result offloading

`core/llm/offloader.py::SandboxOffloader` implements the AgentScope 2.0 `Offloader` protocol: when context compression or tool-result truncation happens, the overflow is no longer silently discarded — it is written into the sandbox at `/workspace/.offload/` (`tool_<id>.txt` / `context_<hash>.txt`), the framework appends the path to the model-facing `<system-reminder>`, and the model can read it back on demand via `Read` / `bash`. Mounted only when sandbox tools are enabled (`SANDBOX_TOOLS_ENABLED=true`, default on); write failures never raise and degrade to an explanatory message.

## Chat sharing

`api/routes/v1/chat_shares.py` provides read-only share links:

| Endpoint | Description |
|---|---|
| `POST /v1/chat-shares` | Create a share link from selected messages; validity `3d / 15d / 3m / permanent` |
| `GET /v1/chat-shares` | Current user's share history |
| `GET /v1/chat-shares/{share_id}` | Anonymous access to shared content (with expiry check) |
| `POST /v1/chat-shares/{share_id}/revoke` / `restore` | Suspend / restore access |
| `DELETE /v1/chat-shares/{share_id}` | Delete the record |

Storage is Redis (`chat_share:*` key groups + TTL) with an in-process memory fallback when Redis is unavailable (dev only). Sharing a chat *inside a team project* is managed separately via `POST /v1/chats/{chat_id}/share` (**Enterprise Edition (EE)** — depends on the team system).

## Other entry points

The same orchestration foundation also powers: response regeneration (`POST /v1/chats/{chat_id}/regenerate`), edit-and-resend (`POST /v1/chats/{chat_id}/edit`), non-streaming `POST /v1/chats/send`, batch execution (`orchestration/batch_orchestrator.py`, see [Automation](automation.md)), and scheduled automation (`orchestration/schedulers/`).

## Source map

| Topic | Path |
|---|---|
| Chat routes / SSE egress | `src/backend/api/routes/v1/chats.py` |
| Run decoupling / Redis Stream / resume | `src/backend/orchestration/chat_run_executor.py`, `api/routes/v1/chat_runs.py` |
| Streaming orchestration | `src/backend/orchestration/workflow.py` |
| Event mapping (reply_stream → SSE) | `src/backend/orchestration/streaming.py` |
| Runtime context assembly | `src/backend/core/chat/context.py` |
| Agent factory | `src/backend/core/llm/agent_factory.py` |
| Middlewares | `src/backend/core/llm/middlewares.py` (pure-function helpers in `core/llm/hooks.py`) |
| Citation extraction | `src/backend/orchestration/citations.py` |
| Citation rendering | `src/frontend/src/utils/citations.ts`, `src/frontend/src/components/citation/` |
| Plan mode | `src/backend/orchestration/subagents/plan_mode.py`, `api/routes/v1/plans.py` |
| Sub-agent tool | `src/backend/core/llm/subagent_tool.py`, `api/routes/v1/agents.py` |
| Title summarization / context compaction / window protection | `src/backend/core/llm/summarizer.py`, `compaction.py`, `core/services/compaction_service.py`, `context_manager.py` |
| Oversized-result offloading | `src/backend/core/llm/offloader.py` |
| Chat sharing | `src/backend/api/routes/v1/chat_shares.py` |
| Follow-up generation | `src/backend/orchestration/followups.py` |
| Frontend stream parsing / follow-up queue | `src/frontend/src/hooks/chatStream.ts`, `useStreaming.ts`, `components/chat/QueuedMessageCard.tsx` |
