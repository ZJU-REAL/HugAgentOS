# Workflow background jobs

run_job executes scripts through the sandbox managed-process API. Each submission stages a separate attempt directory and waits for the runner's startup acknowledgement. Command acceptance alone does not mean the job started. Preparation is limited to 90 seconds; acknowledgement normally arrives within 30 seconds, with additional bounds on external operations. Failures persist an explicit status and error.

Monitoring belongs to the application event loop. Closing a temporary tool thread, timing out a foreground wait, or cancelling the current chat turn does not cancel an already submitted job. Job cancellation targets the exact execution handle, without process-name matching or broad process termination.

Each attempt persists its owner, lease, and sandbox command reference. OpenSandbox allows a new worker to adopt an existing command after its owner's lease expires. If the command state cannot be confirmed, the job becomes interrupted, old callback credentials are revoked, and business work is not automatically repeated. Providers without durable command references use the same conservative fallback.

Explicit resume requires confirmation that the previous process exited or never started. Each attempt gets a fresh callback token. SDK job.map preserves results for done and not_found ledger items and processes the remainder. This does not guarantee exactly-once external side effects for arbitrary scripts; those writes must remain idempotent.

Legacy detached-shell jobs have no reliable process reference and cannot safely resume directly. Cancellation revokes their callback rights and reports that process termination is unconfirmed. Create a new job explicitly after confirming that the old process ended.

Internal callbacks authenticate and update the ledger within the same row-locked transaction, fencing stale tokens and preventing concurrent lost updates. The runner atomically writes a terminal receipt before sending its final callback, allowing monitoring to settle the job even if callback delivery fails.

## Validation

Unit and regression tests live under src/backend/tests/orchestration/test_job*.py. The opt-in real-sandbox test, src/backend/tests/integration/job_managed_smoke.py, requires an existing job for sandbox context. It creates synthetic jobs and a temporary callback endpoint to test concurrent ledger writes, thread exit, isolated cancellation, cross-process adoption, resume, and startup failure. It does not call a model or rerun the original business script.

Before deployment, verify the actual source directory mounted into the container. The working tree and running service may use different checkouts; restarting a container with the wrong mount will not apply these changes.
