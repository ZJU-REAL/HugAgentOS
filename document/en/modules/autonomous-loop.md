# Autonomous Loop

> Last updated: 2026-08-13

The autonomous loop upgrades the agent from single-turn Q&A to **long-running tasks that drive themselves forward across many invocations, keep state externally, and stop on verifiable goal completion**. Alongside normal chat (one-shot) and plan mode (linear multi-step), it is a third execution form: a run-level self-driving loop. The design tracks Codex `/goal` (the Ralph Loop): the goal stays alive across turns and the loop runs until the job is done, while strictly keeping maker ≠ checker — the agent doing the work is never the one grading it.

## Core loop

```
Scout (read-only workspace survey) → Plan (requirement ledger, optional check commands) → per iteration:
  worker runs one round (fresh context, same persistent sandbox, exactly one requirement)
    → machine check (driver itself runs check_cmd; exit code 0 = objectively met)
    → read-only reviewer personally verifies the real output (never trusts self-reports)
    → flip / feed back / stall bookkeeping
  → replan the remaining work when a requirement gets shelved → done when all pass
```

- **Recon-grounded planning**: before work starts, a read-only scout `ls`/`read`/`grep`s the project or /workspace to establish ground truth (what exists, what's missing, what the pitfalls are); the planner decomposes the goal against that survey instead of guessing from the goal text. Task-style loops with an empty workspace skip the scout automatically.
- **Requirement ledger (feature_list.json)**: owned exclusively by the driver; the worker cannot add, delete, or edit entries and is fed exactly one requirement per iteration. Simple goals may decompose into just 1–2 requirements; complex ones cap at 8.
- **Hybrid acceptance**: requirements that can be judged objectively carry a read-only `check_cmd` (e.g. `test -f`, `grep -c`, word-count reconciliation) which the **driver itself executes in the sandbox** — the worker cannot cheat it. A failing check feeds the command output straight into the next round without burning a reviewer run. Semantics and quality are always verified by the **read-only reviewer subagent** opening the real files.
- **Fewer second passes**: a requirement whose machine check passed flips on a single semantic review; purely semantic requirements — and the final requirement whose flip completes the loop — still require an independent second-pass re-check (guards against premature completion).
- **State lives on disk**: every iteration starts with a fresh context (avoiding long-session degradation); artifacts and progress live in the persistent sandbox (`feature_list.json` / `PROGRESS.md` / `handoffs.md`), with a DB mirror of the ledger as the source of truth across restarts and machine changes.
- **Mid-run replanning**: when a requirement stalls for several rounds and gets shelved, the planner re-decomposes the *remaining* work (passed items are immutable) instead of shelving requirements one by one into a "partially done" ending. Replans are capped (`LOOP_MAX_REPLANS`, default 2).

## Stopping: run until done — budgets are optional

The primary stop condition is **all ledger requirements passing**. Budgets (iterations / wall clock / tokens) default to **unlimited** (`0` or omitted = no cap); explicit positive values still apply. The anti-runaway guards are independent of budgets and always active:

1. **Stall guard**: a requirement with N consecutive rounds (orchestration profile `max_attempts_per_requirement`, default 6) without reviewer-affirmed material progress gets shelved (triggering replan / HITL). The reviewer explicitly judges `progress` each round, so healthy multi-round work (e.g. writing a long document chapter by chapter) is not penalized.
2. **Failure circuit breaker**: a worker round that raises or produces nothing is treated as an environment outage — not counted as an attempt, retried after a 30s backoff; only `LOOP_MAX_CONSECUTIVE_INFRA` (default 6) consecutive such rounds trip the loop to `failed`, resumable once the environment recovers.
3. **Hard backstop**: `LOOP_HARD_MAX_ITERS` (default 500) — far beyond any real task, purely an infinite-loop fuse.

The final flip always passes an **independent second-pass review**, and partially-completed endings automatically run one **wrap-up delivery round** (`LOOP_WRAPUP`, default on): consolidate what passed into a usable deliverable and honestly list what remains.

## Terminal states

| State | Meaning |
|---|---|
| `completed` | All ledger requirements passed (including the final second-pass check) |
| `budget_exhausted` | Partially done: requirements shelved with no replan left, or an explicit budget / hard backstop hit |
| `cancelled` | Cancelled by the user |
| `awaiting_human` | HITL enabled and the reviewer requested human input |
| `interrupted` | Interrupted by a service restart (reconciled at startup); resumable from the checkpoint |
| `failed` | Tripped by consecutive infrastructure failures (resumable after recovery) |

## Long-run reliability (harness)

- **No age-based kill**: autonomous-loop runs are exempt from the platform-wide hard run-age cap — a loop still making healthy progress in-process is never reaped for merely running long; orphaned runs are still cleaned up by the stream-quiet rule.
- **Checkpoint resume**: dual-source ledger (DB mirror + sandbox); at startup, orphaned `running` loops are reconciled to `interrupted` (or auto-resumed with `LOOP_AUTO_RESUME=true`).
- **Resume keeps parameters**: the model, reviewer model, worker iteration cap, and thinking level persist with the loop — resuming never silently downgrades to defaults.
- **Mid-run steering**: add an instruction from the plan bar without cancelling; the driver picks it up before the next iteration and injects it into the worker prompt at top priority.

## Model configuration

- **Worker model**: follows the model the user selected in the conversation (same source as normal chat).
- **Reviewer/planner model**: configured independently in the admin console under **Model Management → Role Assignment → Autonomous-loop review & planning** (`loop_reviewer`); falls back to the main agent model when unassigned. The scout, decomposition, reviews, second passes, and verdict rescue all share this role.

## API

| Method | Path | Description |
|---|---|---|
| POST | `/v1/loops` | Create a loop (`goal_spec`; `budget` optional = unlimited) |
| GET | `/v1/loops` / `/v1/loops/{id}` | List / detail |
| POST | `/v1/loops/{id}/start` | Start (SSE stream) |
| POST | `/v1/loops/{id}/resume` | Resume (after HITL approval / interruption; omitted params are restored from the last start) |
| POST | `/v1/loops/{id}/steer` | Queue a mid-run instruction (applies before the next iteration) |
| POST | `/v1/loops/{id}/cancel` | Cancel (reconciles the status directly when no run is active) |
| GET | `/v1/loops/{id}/iterations` | Iteration audit trail |

Frontend entry: the **"Autonomous loop" toggle in the chat input** (`components/chat/InputArea.tsx` + `hooks/useLoopMode.ts`); the plan bar (`components/loop/LoopPlanBar.tsx`) shows the live requirement checklist and review state, with "Resume" and mid-run steering built in. Gated by the `can_run_autonomous_loop` capability (on by default; can be disabled per user / team).

## Related code

- Driver `orchestration/autonomous_loop.py`, planner `orchestration/loop_planner.py`, reviewer `orchestration/subagents/loop_reviewer.py`
- ChatRun integration `orchestration/chat_run_executor.py` (`start_autonomous_loop_run` / startup reconciliation `resume_running_loops`)
- Service `core/services/loop_service.py` (ledger mirror / start params / steering queue), API `api/routes/v1/loops.py`
- Tables `agent_loops` / `loop_iterations`
