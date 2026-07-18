# Chat & Agent Orchestration

> Last updated: July 18, 2026

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
   │         consumes agent.reply_stream(), maps 25 fine-grained events to 8 SSE event kinds
   ▼
SSE follower: chat_run_executor.follow_run_as_sse()
       XRANGE replay + XREAD tail → data: {...}\n\n → browser
       (frontend parsing in src/frontend/src/hooks/useStreaming.ts + App.tsx)
```

### Run decoupling and reconnect/resume

Every sent message creates a `ChatRun` and a background task (`orchestration/chat_run_executor.py`); events are written to a Redis Stream (`maxlen=5000`, 1-hour TTL). The HTTP connection is merely a *follower*, which enables:

| Capability | Endpoint |
|---|---|
| Start a streaming chat | `POST /v1/chats/stream` |
| Resume after refresh / disconnect | `GET /v1/chats/stream/{run_id}?from_offset=N` |
| Probe for an in-flight run | `GET /v1/chats/{chat_id}/active-run` |
| Cancel a run (kills the background task) | `POST /v1/chat-runs/{run_id}/cancel` |

Defensive machinery: a `: heartbeat` SSE comment line every 15 silent seconds (keeps nginx `proxy_read_timeout` and other proxies from cutting the stream); an inactivity watchdog fails the run if the workflow produces no chunk for 600 s (`CHAT_RUN_INACTIVITY_TIMEOUT_SEC`); a periodic reaper collects over-age running runs; `recover_orphan_runs()` cleans up leftovers at startup.

### Agent construction highlights (core/llm/agent_factory.py)

`create_agent_executor()` is the shared factory for every mode (main chat, plan, batch, sub-agents, automation):

- **MCP tools**: after the three-layer filter of catalog + per-user overrides + request context (see [Capability Center](catalog.md)), stable servers reuse the process-level connection pool (`core/llm/mcp_pool.py`); per-request servers (e.g. `retrieve_dataset_content`, which needs per-request HTTP headers) are spawned fresh; the user's self-added private MCP servers are merged in with owner isolation.
- **Skills**: registered as AgentScope Agent Skills via `core/agent_skills/loader.py`, with `view_text_file` allow-listed to read SKILL.md files (see [Agent Skills](agent-skills.md)).
- **File / sandbox tools**: `bash`, `sandbox_put_artifact`, `sandbox_get_artifact` are always registered; Read/Edit/Write/Glob/Grep/Delete/Move/mkdir plus the MySpace tools are gated by `CODE_CAPABILITY_ENABLED` and share one `ReadStateTracker` to keep the "must Read before Edit" invariant.
- **Middlewares** (onion model, `core/llm/middlewares.py`): `DynamicModelMiddleware` (switches the model per chat_mode, see [Model Providers](model-providers.md)), `FileContextMiddleware` (injects uploaded/historical file context), `WorkspacePinHintMiddleware`, `GoalAnchorReminderMiddleware`, `FinishPinGuardMiddleware`.
- **Context compression**: `ContextConfig(trigger_ratio=0.6, tool_result_limit=20000)` plus a structured Chinese compression prompt designed to produce a *resumable ReAct workflow* summary; if the compression call itself fails, `JxOpenAIChatModel.generate_structured_output` returns an L3 synthetic summary so the reply never crashes.
- **Permissions**: every registered tool gets a native `PermissionRule(ALLOW)` seed, preserving AgentScope's built-in dangerous-operation checks (no blanket BYPASS).
- **Iteration caps**: main agent defaults to `max_iters=50`, isolated sub-agents to 10.

## SSE event types and payloads

`orchestration/streaming.py::StreamingAgent` collapses AgentScope 2.0 `reply_stream` events into 8 internal kinds; `workflow.py` and `chats.py::_stream_sse_response` enrich them with chat-level fields before they hit the wire. Events as the frontend sees them:

| `type` | Meaning | Key fields |
|---|---|---|
| `thinking` | Reasoning (delta or stage hint) | `delta` / `message` |
| `content` | Answer text delta | `event: "ai_message"`, `delta`, `chat_id` |
| `tool_call` | A tool invocation (args complete) | `tool_name`, `tool_display_name`, `tool_args`, `tool_id`, plus `subagent_name` for sub-agent calls |
| `tool_result` | Tool invocation result | `tool_name`, `result`, `tool_id`, `citations[]` |
| `tool_pending` | Tool started, args still streaming | `tool_name` |
| `batch_confirm` | Batch plan generated, awaiting user confirmation (human gate) | `plan_id`, `total`, `preview`, `default_template`, `placeholder_keys` |
| `file_confirm` | A tool is suspended awaiting confirmation of a MySpace write | confirmation context; the tool resumes in place after an out-of-band `POST /v1/chats/{chat_id}/file-confirm` |
| `meta` | End-of-turn metadata | `route`, `citations[]`, `sources`, `artifacts`, `workspace_files`, `warnings`, `is_markdown`, `message_id`, `usage` |
| `error` | Failure (mapped to a user-friendly message) | `error`, `chat_id` |
| `heartbeat` | Heartbeat (event-level; a `: heartbeat` comment line also exists) | — |

The stream terminates with `data: [DONE]`. Example frames:

```
data: {"type":"tool_call","tool_name":"internet_search","tool_display_name":"Web Search","tool_args":{"query":"Beijing IC industry"},"tool_id":"call_abc"}

data: {"type":"tool_result","tool_name":"internet_search","result":{...},"tool_id":"call_abc","citations":[{"id":"internet_search-1","title":"...","url":"...","snippet":"...","source_type":"internet"}]}

data: {"type":"content","event":"ai_message","delta":"Based on the search results…","chat_id":"chat_x"}

data: {"type":"meta","route":"main","citations":[...],"usage":{"prompt_tokens":1234,"completion_tokens":456,"total_tokens":1690,"llm_call_count":3},"message_id":"msg_..."}

data: [DONE]
```

After `meta`, `chats.py` persists the assistant message, backfills artifacts, and launches a background follow-up-question generator (`orchestration/followups.py`; results land in the message's `extra_data.follow_up_questions` and are fetched via `GET /v1/chats/{chat_id}/messages/{message_id}/followups`).

## Citation system

Citations make every fact in the answer traceable back to a specific tool result. The chain has three segments:

1. **Prompt contract**: the system prompt (fallback file `prompts/prompt_text/default/system/40_format.system.md`; the active DB version is authoritative at runtime) instructs the model to emit `[ref:tool_name-N]` markers when citing tool data, e.g. `[ref:internet_search-1]`, or `[ref:tool1-N][ref:tool2-M]` for multiple sources.
2. **Backend extraction**: every `tool_result` is normalized by `orchestration/citations.py` into `CitationItem` objects (`id` / `tool_name` / `tool_id` / `title` / `url` / `snippet` / `source_type`). When the same tool is called multiple times in one turn, `extract_citations_with_offset()` keeps ids unique via a per-turn offset table. `source_type` values come from `_SOURCE_TYPE_MAP`: `internet`, `knowledge_base`, `database`, `industry_news`, `ai_news`, `chain_info`, `company_profile` (the latter three come from industry tools — **Enterprise Edition (EE)**).
3. **Frontend rendering**: citations ride on `tool_result` and `meta` events and are persisted with the message; `src/frontend/src/utils/citations.ts` parses inline markers with `/\[ref:([\w]+-\d+)\]/g`, `components/citation/CitationBadge.tsx` renders clickable badges, and `CitationMarkdownBlock` / `CitationHtmlBlock` handle in-body display.

## Plan Mode

Plan Mode splits complex tasks into "generate plan → user reviews/edits → execute step by step", implemented in `orchestration/subagents/plan_mode.py`:

- **Generate** (`astream_generate_plan` / `POST /v1/plans/generate`): a "bare LLM" agent (`disable_tools=True`) produces a structured JSON plan. System-prompt resolution: active `plan_mode` version in the prompt pool → legacy `system/90_plan_mode` part → fallback file `prompts/prompt_text/plan_mode/plan_mode.system.md` → hardcoded minimal prompt.
- **Execute** (`astream_execute_plan` / `POST /v1/plans/{plan_id}/execute`): each step gets its own agent, executed sequentially, with step-level MCP/skill/sub-agent bindings and cancellation (`is_run_cancelled` polling); execution also goes through ChatRun + Redis Stream, so it survives disconnects.
- **Model role**: plan mode prefers the `plan_agent` role and falls back to `main_agent` (the `_mode_role` branch in `agent_factory.py`).
- Unattended modes (plan execution / automation) remove `batch_runner` from the toolkit, since `batch_plan`'s confirmation dialog has no UI in those contexts (`workflow.py::_resolve_batch_runner_visibility`).

## Sub-agents

User-created sub-agents (`api/routes/v1/agents.py`, DB table `UserAgent`)
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

You can reach a sub-agent in two ways:

- **Direct conversation**: when the request context carries `agent_id`, `workflow.py::_astream_subagent_direct` builds an agent from that config, sharing the streaming/memory/citation infrastructure; `meta.route` becomes `subagent:<agent_id>`.
- **Main-agent dispatch**: `core/llm/subagent_tool.py` registers a `call_subagent` tool on the main agent; each sub-agent runs in its own thread with its own event loop (avoiding anyio cancel-scope cross-task errors) and returns text. `@AgentName` mentions in the user message are parsed by `workflow.py::_parse_agent_mentions` (longest-name-first to prevent prefix shadowing); the mention hint is injected into the *current user message*, not the system prompt, to preserve the LLM provider's prefix cache.

## Conversation summarization & context compression

Three complementary layers:

| Layer | Implementation | Trigger |
|---|---|---|
| Chat title summary | `core/llm/summarizer.py::ConversationSummarizer` (`summarizer` model role, `ENABLE_SUMMARY` flag), `POST /v1/summary` | Auto-titling new chats |
| History pre-trim + summary | `core/llm/context_manager.py::ContextWindowManager.manage_context()` trims to the model's context window; dropped messages are condensed by `core/llm/history_summarizer.py::summarize_history()` into a `<conversation_summary>` prepended to the history | Loading history that exceeds the token budget |
| In-session compression | AgentScope 2.0 `ContextConfig` (`trigger_ratio=0.6`); the compression prompt demands a structured, resumable-ReAct-workflow summary (preserving artifact_ids, tool params, TODOs) | Context approaching the window inside the ReAct loop |

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
| Title / history summarization, window management | `src/backend/core/llm/summarizer.py`, `history_summarizer.py`, `context_manager.py` |
| Oversized-result offloading | `src/backend/core/llm/offloader.py` |
| Chat sharing | `src/backend/api/routes/v1/chat_shares.py` |
| Follow-up generation | `src/backend/orchestration/followups.py` |
| Frontend stream parsing | `src/frontend/src/hooks/useStreaming.ts`, `src/frontend/src/App.tsx` |
