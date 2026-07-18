# Autonomous Loop

> Last updated: 2026-07-10

The Autonomous Loop upgrades the agent from single-turn Q&A to a **long-running task that self-advances across many calls, maintains external state, and stops autonomously on a verifiable goal**. Alongside regular chat (single-turn) and plan mode (linear multi-step), it provides a third run mode: a run-level self-driving loop.

## Core loop

```
read state (persistent sandbox files) → agent runs one iteration (fresh context, same persistent sandbox)
  → environment verification (verify_cmd) → evaluator verdict → feedback + compaction handoff → next iteration
```

Each iteration gets a fresh context (avoiding long-session degradation); work artifacts and progress live in files in the persistent sandbox (`PROGRESS.md` / `state.json` / `handoffs.md`) — state lives on disk, not in the context window.

## Two ways to start

| Form | Entry | Goal & verification |
|---|---|---|
| **Conversational** (`self_verify`) | The "Autonomous Loop" toggle in the chat composer (next to "Plan Mode") | Just describe the goal in natural language — no verification config to fill in. Iterations stream back into the current conversation as normal assistant messages (a markdown transcript). |
| **Form** (`verify`) | Lab module → Autonomous Loop | Explicitly fill in `verify_cmd` / target score / budget; suited to cases with a known verification command (e.g. EdgeBench evaluation). |

**Conversational evaluation auto-picks one of two paths** (the worker chooses each round by the nature of the goal; the evaluator routes accordingly):

- **Quantifiable goal** (cost / error / pass-rate…) → the worker **writes its own** `/workspace/verify.sh`; the driver runs it independently for **rule-based scoring** (ground truth, worker self-reports not trusted). Without an explicit threshold, completion comes from **stagnation convergence**: once a valid solution exists and several consecutive rounds show no meaningful gain, it is judged done — ensuring genuine iterate-and-improve rather than exiting on the first valid solution.
- **Qualitative goal** (copy / design / polish…) → the worker produces the deliverable + an evidence note and **writes no script**; at startup one LLM call decomposes the goal into acceptance criteria, then an **independent LLM evaluator** (invoked by the driver each round, never callable by the worker — avoiding self-evaluation bias) checks the criteria one by one to emit `done / continue`.

## Exit decision: environment ground truth first

Reliability order of stop conditions:

1. **Environment verification (primary)**: `goal_spec.verify_cmd` runs in the persistent sandbox; success is decided by exit code + output (tests pass / metric met / target file exists). In conversational mode this script is authored by the worker and run independently by the driver. Anything the environment can verify deterministically does not go through the LLM.
2. **Evaluator (fallback)**: when the goal cannot be fully verified by command, an independent evaluator reads the environment evidence and checks acceptance criteria one by one, emitting a binary verdict (done / continue / off_track). The evaluator is separate from the worker and invoked deterministically by the loop driver each round, avoiding self-evaluation bias.
3. **Budget backstop**: max iterations / wall-clock / cumulative tokens — stops on breach (the guardrail for unattended runs).

On a `done` verdict, a **second verification** (re-running verify) prevents false-positive early delivery.

## Termination outcomes

| Terminal state | Meaning |
|---|---|
| `completed` | Environment verification passed (with second check) |
| `budget_exhausted` | Hit iteration / wall-clock / token budget |
| `cancelled` | User cancelled |
| `awaiting_human` | With HITL enabled, evaluator requested a human (optional) |

## Capabilities

- **Dynamic todos**: the agent maintains an editable todo list in `state.json` each round, checking items off.
- **Self-correction**: several rounds without meaningful score improvement automatically prompts "try a fundamentally different strategy".
- **Crash resume**: after a restart, the persistent sandbox files remain, so the loop resumes from `state.json` (`LOOP_AUTO_RESUME` enabled).
- **Scheduled advancement**: scheduled tasks support a `loop` type that advances the same persistent loop on a cron cycle (rather than creating a new stateless task each time).
- **Human checkpoints (HITL)**: default is fully automatic ("record and continue"); optionally enabled per-loop to pause at key points and `/resume` after human approval.

## API

| Method | Path | Description |
|---|---|---|
| POST | `/v1/loops` | Create a loop (`goal_spec` + `budget`) |
| GET | `/v1/loops` / `/v1/loops/{id}` | List / detail |
| POST | `/v1/loops/{id}/start` | Start (SSE streaming) |
| POST | `/v1/loops/{id}/resume` | Resume (after HITL approval / crash) |
| POST | `/v1/loops/{id}/cancel` | Cancel |
| GET | `/v1/loops/{id}/iterations` | Iteration audit trail |

Frontend entry: the **"Autonomous Loop" toggle in the chat composer** (conversational mode, `components/chat/InputArea.tsx` + `hooks/useLoopMode.ts`) or **Lab module → Autonomous Loop** (form mode, `components/lab/LabPanel.tsx`). Access is gated by the `can_run_autonomous_loop` capability (enabled by default, can be disabled per user / team).

## Related code

- Driver `orchestration/autonomous_loop.py`, evaluator `orchestration/loop_evaluator.py`
- ChatRun integration `orchestration/chat_run_executor.py` (`start_autonomous_loop_run`)
- Service `core/services/loop_service.py`, API `api/routes/v1/loops.py`
- Tables `agent_loops` / `loop_iterations`
