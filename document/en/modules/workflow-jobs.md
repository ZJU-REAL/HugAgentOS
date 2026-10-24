# Workflow background jobs

run_job executes scripts through the sandbox managed-process API. Each submission stages a separate attempt directory and waits for the runner's startup acknowledgement. Command acceptance alone does not mean the job started. Preparation is limited to 90 seconds; acknowledgement normally arrives within 30 seconds, with additional bounds on external operations. Failures persist an explicit status and error.

Monitoring belongs to the application event loop. Closing a temporary tool thread, timing out a foreground wait, or cancelling the current chat turn does not cancel an already submitted job. Job cancellation targets the exact execution handle, without process-name matching or broad process termination.

Each attempt persists its owner, lease, and sandbox command reference. OpenSandbox allows a new worker to adopt an existing command after its owner's lease expires. If the command state cannot be confirmed, the job becomes interrupted, old callback credentials are revoked, and business work is not automatically repeated. Providers without durable command references use the same conservative fallback.

Explicit resume requires confirmation that the previous process exited or never started. Each attempt gets a fresh callback token. SDK job.map preserves results for done and not_found ledger items and processes the remainder. This does not guarantee exactly-once external side effects for arbitrary scripts; those writes must remain idempotent.

Legacy detached-shell jobs have no reliable process reference and cannot safely resume directly. Cancellation revokes their callback rights and reports that process termination is unconfirmed. Create a new job explicitly after confirming that the old process ended.

Internal callbacks authenticate and update the ledger within the same row-locked transaction, fencing stale tokens and preventing concurrent lost updates. The runner atomically writes a terminal receipt before sending its final callback, allowing monitoring to settle the job even if callback delivery fails.

## Derived main agents

The workflow SDK `agent(prompt, ...)` uses the shared main-agent factory and execution loop. Each call starts an independent context and inherits the submitting agent's model, skills, plugin capabilities, MCPs, knowledge bases, visible subagents, project and workspace configuration. It does not copy chat history. Derived agents and their subagents share the original conversation workspace and use the `full` approval preset without operation approval prompts. User access checks, data isolation and job budgets remain in effect.

Derived agents can delegate through `call_subagent`. `run_job` and chat interaction tools remain exclusive to the submitting agent, preventing nested workflow submission. Concurrent writers must use non-overlapping workspace paths.

Omitting `tools` inherits the parent's MCPs. A non-empty explicit list selects initial MCPs within the submitting conversation's scope; skills and subagents remain available. Callback authority and remaining budgets are checked throughout execution. Cancellation or wall-clock exhaustion cancels the derived task and awaits child cleanup. Older jobs without a saved main-agent configuration must be resubmitted; there is no lightweight-executor fallback.

Derived instances use the main-agent iteration policy without lightweight or subagent turn caps. Each complete task executes once. Invalid output, execution failure or an uncertain callback result never automatically repeats potentially completed side effects. Inspect existing actions before explicitly resuming.

Windows local jobs use `.job/<full attempt ID>/` to reduce directory depth and keep packaged Python job SDK imports below the traditional path-length limit. Each execution retains its complete unique attempt ID; resume and cancellation use the persisted actual directory and exact process reference.
