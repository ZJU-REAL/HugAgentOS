# Model Providers

> Last updated: 2026-08-24

HugAgentOS talks to any large-language-model endpoint that speaks the **OpenAI-compatible protocol** (vLLM, Ollama, DashScope, DeepSeek, API gateways, …). Model configuration is database-driven: administrators register *model providers* in the Config console and bind them to *roles* (main reasoning, summarization, embeddings, …); everything flows through `ModelConfigService` with a 30-second TTL cache, so configuration changes take effect without a restart. The `MODEL_URL` / `API_KEY` / `BASE_MODEL_NAME` environment variables remain only as compatibility fallbacks and for injection into MCP subprocesses.

## Configuring models: providers + roles

Two tables (`core/db/models.py`): `model_providers` (base_url / api_key / model_name / extra_config / is_active) and `model_role_assignments` (role_key → provider_id). Roles are defined in `core/db/model_repository.py::ROLE_DEFINITIONS`:

| role_key | Purpose | Type |
|---|---|---|
| `main_agent` | Main agent reasoning (required; chat endpoints return 503 if missing) | chat |
| `summarizer` | Chat title summary + classification | chat |
| `followup` | Follow-up question generation | chat |
| `memory` | Memory extraction (mem0) | chat |
| `embedding` | Text embeddings (KB / memory) | embedding |
| `reranker` | Retrieval re-ranking | reranker |
| `chart` | Chart-code generation | chat |
| `plan_agent` | Plan-mode reasoning (falls back to main_agent) | chat |
| `code_exec` | Code-execution reasoning (optional ops override, unused by default) | chat |
| `loop_reviewer` | Autonomous-loop review and planning (falls back to main_agent) | chat |
| `kb_wiki` | Knowledge-base Wiki entity extraction (offline batch; a cheap model is usually the right choice) | chat |
| `vision` | Image understanding (vision bridge) — see below | chat |

`extra_config` supports keys such as `temperature` / `max_tokens` / `timeout` / `context_length` (context window, used for compression thresholds) / `supports_reasoning_effort` (whether thinking-effort levels are supported) / `supports_vision` (whether the model reads images natively).

### Context-window auto-detection

`context_length` drives history trimming and the auto-compaction threshold. Getting it wrong makes a model compact at half its real window, so there is **no runtime fallback** — a model left with an empty value cannot serve traffic. To spare operators the manual lookup, the field is filled in from the upstream when a provider is saved with it empty, and the model form carries an "Auto-detect" button that triggers the same discovery on demand (endpoint: `POST /v1/models/providers/detect-context`).

Sources are tried in order of trust, stopping at the first hit:

| Source | How | Confidence |
|---|---|---|
| Provider model listing | `GET {base_url}/models`, reading `max_model_len` (vLLM / SGLang), `context_length` (OpenRouter and friends) or `max_input_tokens` (LiteLLM gateways) off the matching entry | High |
| Ollama | The native `POST /api/show` response's `model_info.<architecture>.context_length` | High |
| Upstream limit error | One request whose `max_tokens` far exceeds any real cap: the upstream rejects it during parameter validation (no inference, no tokens billed) and often names the real window in the error text | Medium |
| Model-family inference | Matching the model name against known families' typical windows | Low |

Two safety boundaries:

- **Only signals that unambiguously name the context length are adopted.** When an upstream reports nothing but a `max_tokens` cap — which on most vendors is far smaller than the context window — the number is recorded in the detection trail for a human to read but is **never** stored as the window; adopting it would recreate the compact-at-half-window bug.
- **No over-long-prompt probe.** It is the most reliable signal on self-hosted vLLM, but gateways may genuinely accept and run inference over a six-figure-token prompt, which costs real money.
- A manually entered value **always wins**: detection only ever writes into an empty field. Auto-filled values are marked with `context_length_source` and shown in the console as "not yet confirmed"; saving the provider once confirms them.

## Vision bridge: giving text-only models eyes

Flagship text models such as DeepSeek, GLM, and the text-only Qwen builds cannot see images. The vision bridge does not replace the main model — it hangs a transcription channel off the side of it:

```
image → multimodal model → structured text evidence → injected into the main model's context
```

The main model only ever consumes text. Enabling it takes one step: assign a multimodal model to the **`vision` role** in Model Management (qwen-vl, GLM-4V, gpt-4o, gemini — any OpenAI-compatible endpoint works; the Anthropic and Gemini native protocols are supported too).

Resolution order:

1. The model actually serving this conversation has `supports_vision` set → **the image is passed through untouched** (highest fidelity);
2. Otherwise, if a `vision` role is assigned → the bridge transcribes it;
3. Neither → the model is told explicitly that it cannot read images and the user is pointed at Model Management, rather than the image being silently dropped.

**The transcription contract** is six fixed fields: `summary`, `ocr` (full transcription plus lines), `layout` (regions in reading order), `semantics` (scene / intent / entities / relations), `visual` (dominant colors / style / notes), and `uncertainty` (anything unreadable or ambiguous). It deliberately carries **no** pixel coordinates and **no** confidence scores — those are exactly the fields vision models fabricate most convincingly. Whatever cannot be read goes into `uncertainty` instead of being invented.

**Where it applies** (assigning the role turns all of this on at once):

| Situation | Behaviour |
|---|---|
| User uploads an image or pastes a clipboard image into the chat composer | Transcribed and injected at the start of each turn |
| Agent reads an image in the sandbox or "My Space" | `Read` returns `type=image_evidence` instead of `type=binary` |
| A tool returns an image (e.g. chart rendering) | Transcribed and substituted, so the agent can check its own output |
| A group-chat bot receives an image attachment | `channel_read_attachment` points at `view_image` |
| The agent needs a specific detail from an image | `view_image(file_path/file_id, focus="…")` takes a second, targeted look |

**Cost and limits**: every new image costs one multimodal call (a few seconds of added time-to-first-token). Evidence is cached per image + model + focus (7 days by default), so looking at the same picture again is free. Size limit, cache TTL, concurrency, and timeout are the `VISION_*` environment variables.

**Security**: text inside an image is treated as **untrusted external input** — the prompt requires the vision model to treat the image strictly as data and never follow instructions found inside it, and the injected evidence is wrapped in an `<image-evidence>` fence that says where it came from. Images are validated by file-header magic bytes, so content that claims to be an image but isn't is rejected. This is mitigation, not a guarantee: images from untrusted sources still deserve caution.

### Management API (api/routes/v1/models.py, CONFIG_TOKEN)

| Endpoint | Description |
|---|---|
| `GET/POST/PUT/DELETE /v1/models/providers...` | Provider CRUD; saving performs a **real connectivity pre-check** (hits `/chat/completions`, `/embeddings`, or `/rerank` by type) and returns 400 on failure; base_url is normalized to `…/v1`; api_key is masked in responses |
| `POST /v1/models/providers/{id}/test`, `POST /v1/models/providers/test` | Connectivity test for saved / unsaved configs |
| `GET /v1/models/roles`, `PUT/DELETE /v1/models/roles/{role_key}` | Role assignment (validates provider type matches the role; referenced providers cannot be deleted) |
| `GET /v1/models/export`, `POST /v1/models/import` | Cross-environment migration of model config |
| `GET /v1/models/capabilities` | **Public endpoint**: exposes only the `main_agent.supports_reasoning_effort` boolean, which drives the frontend "thinking: medium/high/max" switch |

All writes call `ModelConfigService.invalidate_cache()`; the whole process picks the change up within 30 seconds.

## JxOpenAIChatModel (core/llm/chat_models.py)

Runtime model instances are built by `make_chat_model()`, which returns `JxOpenAIChatModel` — a subclass of AgentScope 2.0's `OpenAIChatModel` that adds three things the stock class cannot do:

1. **Streaming read timeout**: generating long tool-call arguments can stay silent for 130–160 s per chunk; a custom `httpx.AsyncClient` raises the read timeout to 600 s (`STREAM_READ_TIMEOUT_S`) while connect/write/pool keep the provider-configured timeout.
2. **Thinking-chain switch**: OpenAI-compatible endpoints like Qwen / MiniMax control thinking via `extra_body.chat_template_kwargs` (`enable_thinking` / `thinking` / `reasoning_effort`), injected on every call.
3. **Structured-output fallback (L3)**: context compression goes through `generate_structured_output()`; some models return malformed JSON, which would crash the whole `reply()`. The subclass catches the exception and returns the `L3_SYNTHETIC_METADATA` placeholder summary so compression still lands and the conversation continues.

Retry policy: the model layer runs with `max_retries=0`; the agent layer's `ModelConfig(max_retries=3)` owns retries exclusively, avoiding multiplicative double-retrying.

## Dynamic model switching (chat_mode)

Each frontend message may carry `chat_mode` (`turbo / fast / medium / high / max`); before the reply starts, `core/llm/middlewares.py::DynamicModelMiddleware` (`on_reply`) hot-swaps `agent.model`:

```
chat_mode → hooks._resolve_chat_mode(agent.state)
          → hooks._get_main_model(mode)        # process-level instance cache, invalidated by ModelConfigService.version
   turbo/fast → disable_thinking=True (turbo additionally narrows the agent to retrieval-only tools at assembly time)
   medium → thinking on (with effort=medium when supports_reasoning_effort)
   high/max → reasoning_effort=high/max (endpoint must declare support)
```

Legacy clients that only send `enable_thinking` are mapped to medium / fast. This is the AgentScope 2.0 incarnation of the old 1.x "pre_reply dynamic-model hook" — the hook factories were refactored into middlewares, and `core/llm/hooks.py` now only hosts pure-function helpers.

## Environment variables and MCP subprocess injection

| Variable | Status |
|---|---|
| `MODEL_URL` / `API_KEY` / `BASE_MODEL_NAME` | No longer read by the main path; kept in `core/config/settings.py` and a few legacy paths (e.g. `internal_batch.py`, `kb_processing.py`) as fallbacks |
| MCP subprocesses | `ModelConfigService.get_mcp_env_overlay()` maps the `main_agent` / `chart` / `embedding` / `reranker` roles back to the legacy variable names (`MODEL_URL`, `OPENAI_API_KEY`, `MEM0_EMBED_*`, `RERANKER_*`, …); long-lived MCP containers use `core/config/runtime_env.py::get_runtime_value()` to consult the DB first and fall back to env, so console model changes reach MCP tools immediately |

Full variable list: [Environment Variables](../deployment/environment-variables.md).

## Personal API keys

Users can issue personal keys for programmatic access to the platform API (`api/routes/v1/api_keys.py` + `core/services/api_key_service.py`):

- Plaintext keys look like `sk-jx-<random>` and are **returned exactly once at creation**; the DB stores only a SHA256 hash + prefix. Expiry options 7/30/90/180/365 days or never; enable/disable and revoke supported.
- Usage: `Authorization: Bearer sk-jx-...`; the auth layer (`core/auth/backend.py`) recognizes the prefix and resolves the key to a user context via `resolve_api_key`, checking enabled/unrevoked/unexpired.
- Gated by the `can_use_api_key` permission flag (`users_shadow.metadata`). The Community Edition simply switches it on; **centralized per-user governance by an organization administrator is Enterprise Edition (EE)**.

Endpoints: `GET/POST /v1/me/api-keys`, `PATCH /v1/me/api-keys/{id}` (toggle), `DELETE /v1/me/api-keys/{id}` (revoke).

## Billing and usage

Token usage is accumulated into the end-of-stream `meta.usage` (`orchestration/streaming.py` sums `ModelCallEndEvent`s) and persisted with the message. Two console report groups sit on top (`CONFIG_TOKEN`):

- **Usage logs** `api/routes/v1/admin_usage_logs.py`: `GET /v1/admin/usage-logs` (detail), `/summary` (aggregate), `/models` (distinct model names).
- **Billing reports** `api/routes/v1/admin_billing.py` (**Enterprise Edition (EE)** — part of the full admin console): `GET /v1/admin/billing/summary` (cost aggregated by user/model), model pricing CRUD (`/pricing`, input/output unit prices, currency), `GET /v1/admin/billing/export` (CSV cost export).

Community Edition users can see their own token usage; organization-level billing aggregation, pricing management, and cost export belong to EE, with quota enforcement on the EE roadmap.

## Routing strategy (ROUTER_STRATEGY)

`orchestration/strategy.py`: `ROUTER_STRATEGY=main_only` (default) always routes to the main agent; `llm_router` is a reserved placeholder that currently also falls back to `MainOnlyStrategy` (safe by default). Actual multi-agent dispatch happens via `@mentions` + the `call_subagent` tool — see [Chat & Agent Orchestration](chat.md).

## Source map

| Topic | Path |
|---|---|
| Model factory / JxOpenAIChatModel | `src/backend/core/llm/chat_models.py` |
| Role resolution service (DB + cache) | `src/backend/core/services/model_config.py` |
| Role definitions / provider repository | `src/backend/core/db/model_repository.py` |
| Management API | `src/backend/api/routes/v1/models.py` |
| Dynamic-model middleware | `src/backend/core/llm/middlewares.py::DynamicModelMiddleware`, helpers in `core/llm/hooks.py` |
| MCP env injection | `src/backend/core/services/model_config.py::get_mcp_env_overlay`, `core/config/runtime_env.py` |
| Personal API keys | `src/backend/api/routes/v1/api_keys.py`, `core/services/api_key_service.py`, `core/auth/backend.py` |
| Usage logs / billing | `src/backend/api/routes/v1/admin_usage_logs.py`, `api/routes/v1/admin_billing.py` |
| Routing strategy | `src/backend/orchestration/strategy.py` |
| Fail-fast on missing main model | `src/backend/api/routes/v1/chats.py::_ensure_main_model_configured` |
