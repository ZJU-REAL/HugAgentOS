# Automation & Batch Execution

> Last updated: 2026-06-11

HugAgentOS ships three built-in mechanisms for turning a single instruction into repeatable or bulk productivity, all part of the **Community Edition (CE)**:

| Capability | One-liner | Backend entry |
|---|---|---|
| Scheduled automation | Run a prompt or a plan automatically on a cron schedule (or once / manually) | `api/routes/v1/automations.py` + `orchestration/schedulers/automation_scheduler.py` |
| Plan mode | AI first decomposes a complex task into structured steps, then executes them after confirmation | `api/routes/v1/plans.py` + `orchestration/subagents/plan_mode.py` |
| Batch execution | "Do the same thing to each item in a set" packaged as a confirmable batch plan | `api/routes/v1/batch.py` + `orchestration/batch_orchestrator.py` + `mcp_servers/batch_runner_mcp/` |

## Scheduled automation tasks

### Task model

Tasks live in the `scheduled_tasks` table (`ScheduledTask` in `core/db/models/automation.py`). Key fields:

| Field | Description |
|---|---|
| `task_type` | `prompt` (run a prompt) or `plan` (run an existing plan; `plan_id` ownership is validated) |
| `cron_expression` | Standard cron expression, validated with `croniter.is_valid()` on create / update |
| `schedule_type` | `recurring` / `once` / `manual` (manual tasks never fire automatically) |
| `timezone` | Defaults to `Asia/Shanghai`; the next fire time is computed in that timezone and stored as UTC (`automation_service.py::compute_next_run`) |
| `enabled_mcp_ids` / `enabled_skill_ids` / `enabled_kb_ids` / `enabled_agent_ids` | Restrict the tools / skills / knowledge bases / sub-agents available to the task; `None` means the default full set, an explicit empty list means strictly none |
| `max_runs` / `max_failures` | Run-count cap; consecutive-failure threshold (auto-disable after 3 by default) |

### REST API (`/v1/automations`)

| Method / path | Description |
|---|---|
| `POST ""` / `GET ""` / `GET /{task_id}` / `PATCH /{task_id}` / `DELETE /{task_id}` | Task CRUD |
| `POST /{task_id}/pause` / `resume` / `trigger` | Pause / resume / fire manually |
| `POST /{task_id}/activate-sidebar` | Activate the task's chat group in the sidebar |
| `GET /{task_id}/runs` | Run history (`scheduled_task_runs` table: status, duration, result summary, linked chat) |
| `GET /notifications/list` / `POST /notifications/read` / `delete` | Automation result notifications (Redis list `jx:notifications:{user_id}`, latest 50 kept, 7-day TTL) |

### Scheduling mechanics (automation_scheduler.py)

`orchestration/schedulers/automation_scheduler.py` is an asyncio polling scheduler started with the backend:

- **Polling**: every 15 seconds (plus 0–5 s random jitter) it queries the DB for tasks whose `next_run_at` is due.
- **Distributed lock**: before firing, it acquires the Redis lock `jx:auto:lock:{task_id}` (TTL 900 s) so multiple instances never double-fire.
- **Advance before firing**: `next_run_at` is pushed to the next period **before** execution — the schedule moves on regardless of success, failure, or a mid-flight kill (mirroring real cron), eliminating the death spiral where a stuck `running` row leaves `next_run_at` in the past and every poll re-fires the same task.
- **Execution timeout**: a single run is capped at 800 s wall clock, strictly below the lock TTL so the timeout fires before the lock expires.
- **Failure governance**: when consecutive failures reach `max_failures` (default 3), the task is automatically set to `disabled`.
- **Startup recovery**: after a restart, runs stuck in `running` for more than 30 minutes are marked `failed` (and their parent task's schedule advanced); missed one-shot tasks are then re-fired.

Each execution produces a **real chat session**: prompt tasks reuse the main chat workflow `orchestration/workflow.py::astream_chat_workflow`, fully preserving tool calls, citations, and artifact files; plan tasks go through `orchestration/subagents/plan_mode.py::astream_execute_plan` and persist a plan execution snapshot. Session titles are prefixed `[自动化]`, and the run history offers one-click "view conversation". After completion, the user is notified via Redis notifications plus sidebar activation.

### Frontend

The automation management UI lives under the Lab module: `src/frontend/src/components/lab/` contains `AutomationPanel.tsx` (list), `AutomationCreateModal.tsx` (creation, with cron configuration and capability selection), `AutomationCard.tsx`, and `AutomationDetailPage.tsx` (detail + run history). `src/frontend/src/components/automation/RunTimelinePanel.tsx` renders the date-grouped run timeline on the chat side. State is held in `stores/automationStore.ts` (task management) and `stores/automationChatStore.ts` (sidebar chat groups).

## Plan mode

Plan mode decomposes a complex task into structured steps before executing them, in two phases (`orchestration/subagents/plan_mode.py`):

1. **Generate (Phase 1)**: `POST /v1/plans/generate` (SSE streaming) — the AI analyzes the task description and produces a plan draft with a title, description, and step list; each step can declare `expected_tools` / `expected_skills` / `expected_agents`.
2. **Execute (Phase 2)**: `POST /v1/plans/{plan_id}/execute` (SSE streaming) — steps run sequentially, each with its own agent; step status, tool-call logs, and AI output are persisted incrementally.

Data model (`core/db/models/agent.py`): `Plan` (state machine `draft → approved → running → completed/failed/cancelled`) + `PlanStep` (ordered by `step_order`, recording `result_summary` / `tool_calls_log` / `ai_output` / `error_message`).

REST API (`/v1/plans`, `api/routes/v1/plans.py`): `GET ""` list, `GET/PATCH/DELETE /{plan_id}` detail / edit (steps can be edited before confirmation) / delete, `POST /{plan_id}/cancel` cancel. The plan-mode system prompt supports DB version-pool management with a filesystem fallback at `prompts/prompt_text/plan_mode/plan_mode.system.md` — see [Prompt System](prompts.md).

Plans can also be scheduled: after creating an automation task with `task_type=plan`, every trigger resets a completed / failed plan back to `approved`, clears step states, and reruns it from the top.

## Batch execution

Batch execution addresses "do the same thing to N items" (rate 10 companies one by one, analyze every row of an Excel sheet, extract key clauses from each contract). The flow has two phases:

```
User message (with batch intent)
  → LLM calls the batch_plan tool (mcp_servers/batch_runner_mcp/server.py)
    → the MCP process calls back POST /v1/internal/batch/resolve (internal_batch.py, BACKEND_INTERNAL_TOKEN auth)
       · parse uploaded files (xlsx / word) → structured items
       · or LLM-split a natural-language enumeration → items
       · infer a default prompt template (with placeholders) and persist the BatchPlan
  → backend pauses the SSE stream; frontend shows a confirmation dialog (BatchConfirmModal: review items, edit the template)
  → POST /v1/batch/{plan_id}/confirm
  → GET /v1/batch/{plan_id}/stream (SSE) starts the BatchOrchestrator (Phase 2, deterministic execution)
```

### Phase 1: plan generation (LLM-driven)

The `batch_plan` tool description hard-codes strong trigger rules: whenever the message contains expressions like "批量 / 分别 / 逐个 / 每一个…" or enumerates ≥2 parallel objects in one sentence, the model **must** call the tool instead of answering directly, then end its turn immediately and wait for confirmation. The tool returns the `plan_id`, total item count, a preview of the first 3 items, the inferred default template, and the available placeholders.

### Phase 2: deterministic execution (BatchOrchestrator)

After confirmation, `orchestration/batch_orchestrator.py` iterates `plan.items` serially, **spawning a fresh ReActAgent per item** (with batch_runner disabled to prevent recursion) and persisting each result back to the DB:

- **Refresh survival**: execution runs in a detached background asyncio task; an SSE client disconnect (page refresh, tab switch) does not interrupt it. On reconnect, a still-running task is tailed for new events, while a finished one replays all results from the DB.
- **Retries & skipping**: a failed item is retried with exponential back-off up to `max_retries`; once exhausted it is recorded as `skipped` and the loop continues.
- **Cancellation**: `POST /{plan_id}/cancel` sets the `cancelled` flag and the orchestrator stops at the next item boundary; `POST /{plan_id}/cancel-and-resume` (SSE) cancels the batch, deletes the assistant turn that triggered it, and re-streams the original user message with batch_plan disabled — a one-click escape for "actually, I didn't want a batch".

Plan state machine: `pending → confirmed → running → done / failed / cancelled` (`batch_plans` table). Frontend components: `src/frontend/src/components/batch/BatchConfirmModal.tsx` (confirmation / template editing) and `BatchProgressPanel.tsx` (per-item progress stream), with state in `stores/batchStore.ts`.

## Typical scenarios

**Daily 8 a.m. industry briefing (prompt automation)**

```json
POST /v1/automations
{
  "task_type": "prompt",
  "name": "Industry morning brief",
  "prompt": "Search the last 24 hours of major EV-industry news and produce a bulleted briefing",
  "cron_expression": "0 8 * * *",
  "schedule_type": "recurring",
  "enabled_mcp_ids": ["internet_search"]
}
```

Every morning a `[自动化] Industry morning brief` chat session appears automatically, with notifications in the bell and the sidebar.

**Weekly report pipeline (plan automation)**: build and validate a three-step plan ("pull data → aggregate analysis → export Word") in plan mode, then create a task with `task_type=plan` and `cron_expression="0 17 * * 5"` to rerun the whole plan every Friday at 5 p.m.

**Excel batch analysis (batch execution)**: upload a 200-row company list as xlsx and type "give a business-risk assessment for each row's company" — the model calls `batch_plan`, you confirm the template ("Assess the business risk of {{company_name}}…"), and execution proceeds row by row, surviving page refreshes, with automatic retries for failed rows.

## Source map

| Topic | Path |
|---|---|
| Automation REST API | `src/backend/api/routes/v1/automations.py` |
| Scheduler (polling / locks / recovery) | `src/backend/orchestration/schedulers/automation_scheduler.py` |
| Automation service (cron computation / state machine) | `src/backend/core/services/automation_service.py` |
| Task / run-record models | `src/backend/core/db/models/automation.py` |
| Plan REST API | `src/backend/api/routes/v1/plans.py` |
| Plan generation / execution orchestration | `src/backend/orchestration/subagents/plan_mode.py`, `src/backend/core/services/plan_service.py` |
| Plan / step models | `src/backend/core/db/models/agent.py` |
| Batch REST + SSE | `src/backend/api/routes/v1/batch.py` |
| Internal batch resolver | `src/backend/api/routes/v1/internal_batch.py` |
| batch_plan MCP tool | `src/backend/mcp_servers/batch_runner_mcp/server.py`, `_planner.py` |
| Batch orchestrator | `src/backend/orchestration/batch_orchestrator.py` |
| Frontend automation UI | `src/frontend/src/components/lab/AutomationPanel.tsx` etc., `src/frontend/src/components/automation/RunTimelinePanel.tsx` |
| Frontend batch UI | `src/frontend/src/components/batch/`, `src/frontend/src/stores/batchStore.ts` |
| Frontend state | `src/frontend/src/stores/automationStore.ts`, `automationChatStore.ts` |

Further reading: [Chat System](chat.md) · [MCP Tools](mcp-tools.md) · [Canvas & Artifacts](canvas-artifacts.md)
