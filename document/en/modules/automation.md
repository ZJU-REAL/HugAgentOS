# Automation & Plan Mode

> Last updated: 2026-09-30

HugAgentOS ships two built-in mechanisms for turning a single instruction into repeatable productivity, all part of the **Community Edition (CE)**:

| Capability | One-liner | Backend entry |
|---|---|---|
| Scheduled automation | Run a prompt or a plan automatically on a cron schedule (or once / manually) | `api/routes/v1/automations.py` + `orchestration/schedulers/automation_scheduler.py` |
| Plan mode | AI first decomposes a complex task into structured steps, then executes them after confirmation | `api/routes/v1/plans.py` + `orchestration/subagents/plan_mode.py` |

## Scheduled automation tasks

### Desktop hybrid mode

Local scheduled tasks belong to the signed-in cloud account. The account mapping verifies ownership of local projects and conversations; internal user IDs in the local and cloud databases do not need to match. Task creation and management check the current account and tool authorization. Requests from the previous account cannot continue operating on local tasks after logout, an account switch, or a cloud-server switch.

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
- **Distributed lock**: before firing, it acquires the Redis lock `jx:auto:lock:{task_id}` (TTL 1800 s) so multiple instances never double-fire.
- **Advance before firing**: `next_run_at` is pushed to the next period **before** execution — the schedule moves on regardless of success, failure, or a mid-flight kill (mirroring real cron), eliminating the death spiral where a stuck `running` row leaves `next_run_at` in the past and every poll re-fires the same task.
- **Execution timeout**: a single run is capped at 1500 s wall clock, strictly below the lock TTL so the timeout fires before the lock expires.
- **Failure governance**: when consecutive failures reach `max_failures` (default 3), the task is automatically set to `disabled`.
- **Startup recovery**: after a restart, runs stuck in `running` for more than 40 minutes are marked `failed` (and their parent task's schedule advanced); missed one-shot tasks are then re-fired.

Each execution creates and immediately links a **real conversation** through the shared ChatRun executor. Prompt runs stream text, thinking and tools; plan runs stream step progress. Opening a running conversation or refreshing the page follows and replays the same event stream as ordinary chat. Users can stop execution from the conversation; prompt runs also accept additional instructions through the durable steering queue at the next safe model boundary. Stopping does not increase consecutive failures, and timeouts cancel the child run. Final messages, artifacts, citations and delivery remain owned by the shared executor and scheduler outcome handler. Conversation titles use the task name without an automation prefix. Older executions started before live-stream support cannot replay events that were never recorded.

Scheduled prompts and ordinary conversations share admission, capability resolution, runtime context construction and the main agent execution loop. If a late instruction starts a successor through the main executor, the scheduled outcome waits for its final result. Timeout or scheduler shutdown cancels the current successor.

### Frontend

Scheduled Tasks is a dedicated module under src/frontend/src/components/automation/. Its sidebar groups conversations by task ID and shows one entry per scheduled task, keeping distinct tasks separate even when their names match. New executions update the entry's latest activity time; clicking it opens the newest execution. The right control panel retains individual executions, and choosing a historical run keeps the same task selected in the sidebar. Setup conversations remain separate, and empty setup conversations are hidden. The oldest conversation anchors each entry, preserving its ordering, pin, favorite, and rename actions. Export and delete apply to the selected execution, or the newest execution when the task is not open; deleting one conversation does not delete the scheduled task. All Tasks opens configuration, status, and run history in the content area. Creating through chat first persists a conversation owned by Scheduled Tasks, references the installed task-management plugin, and prefills an editable template. Task conversations are excluded from the ordinary chat sidebar.

Execution conversations use /automation/:taskId/conversations/:chatId, with new as the task segment for setup conversations. Reloads, browser history, search hits, and notifications preserve task ownership. Search opens the exact matching conversation instead of the latest run. RunTimelinePanel groups executions by date. automationStore owns the single task list; automationChatStore owns the current run group and task pin/favorite preferences.

## Plan mode

Plan mode decomposes a complex task into structured steps before executing them, in two phases (`orchestration/subagents/plan_mode.py`):

1. **Generate (Phase 1)**: `POST /v1/plans/generate` (SSE streaming) — the AI analyzes the task description and produces a plan draft with a title, description, and step list; each step can declare `expected_tools` / `expected_skills` / `expected_agents`.
2. **Execute (Phase 2)**: `POST /v1/plans/{plan_id}/execute` (SSE streaming) — steps run sequentially, each with its own agent; step status, tool-call logs, and AI output are persisted incrementally.

Data model (`core/db/models/agent.py`): `Plan` (state machine `draft → approved → running → completed/failed/cancelled`) + `PlanStep` (ordered by `step_order`, recording `result_summary` / `tool_calls_log` / `ai_output` / `error_message`).

REST API (`/v1/plans`, `api/routes/v1/plans.py`): `GET ""` list, `GET/PATCH/DELETE /{plan_id}` detail / edit (steps can be edited before confirmation) / delete, `POST /{plan_id}/cancel` cancel. The plan-mode system prompt supports DB version-pool management with a filesystem fallback at `prompts/prompt_text/plan_mode/plan_mode.system.md` — see [Prompt System](prompts.md).

Plans can also be scheduled: after creating an automation task with `task_type=plan`, every trigger resets a completed / failed plan back to `approved`, clears step states, and reruns it from the top.

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

Every morning an Industry morning brief execution conversation is created. The sidebar keeps one task entry, while the right panel lets users choose results from different dates. Notifications appear in the bell.

**Weekly report pipeline (plan automation)**: build and validate a three-step plan ("pull data → aggregate analysis → export Word") in plan mode, then create a task with `task_type=plan` and `cron_expression="0 17 * * 5"` to rerun the whole plan every Friday at 5 p.m.

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
| Frontend automation UI | `src/frontend/src/components/lab/AutomationPanel.tsx` etc., `src/frontend/src/components/automation/RunTimelinePanel.tsx` |
| Frontend state | `src/frontend/src/stores/automationStore.ts`, `automationChatStore.ts` |

Further reading: [Chat System](chat.md) · [MCP Tools](mcp-tools.md) · [Canvas & Artifacts](canvas-artifacts.md)
