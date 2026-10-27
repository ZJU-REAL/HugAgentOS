# CLI ACP integration

HugAgentOS CLI exposes native Agent Client Protocol (ACP) v1 over stdio. Product code lives in the CLI and backend, independent of evaluation code. The default protocol in Node SDK 2.0.2 is v1.

## Install and launch

Initialize the local account and model using the [single-machine installation guide](../deployment/quick-install.md), then build from the checkout:

```bash
(cd packages/chat-sdk && npm ci && npm run build)
(cd packages/cli && npm ci && npm run build)
npm install -g ./packages/cli
hugagent --mode local --output acp
```

Requires Node.js 20+, Python 3.11+ and a working `hugagent-server`. Alternatively set `HUGAGENT_REPO` and `HUGAGENT_PYTHON`. Account, model and shared engine configuration live under `HUGAGENT_HOME` (default `~/.hugagent`). initialize does not start the engine; session/new attaches or starts it.

Client command: `["hugagent", "--mode", "local", "--output", "acp"]`. JSON-RPC uses stdin/stdout; diagnostics use stderr. Do not combine ACP with `-p`, a prompt, resume, continue or `--auto-approve`. Only local mode is supported for this integration; remote cloud/device services do not expose local leases.

## Capabilities and limits

| Feature | Behavior |
| --- | --- |
| Sessions | new/load/resume/list/close/delete; load replays all history pages; resume does not replay |
| Conversation | Multiple turns/sessions; overlapping prompts in one session are rejected; streaming text, thoughts, tools, plans and usage |
| Configuration | Session-scoped model/mode; set_mode and set_config_option send synchronized notifications |
| Permissions | ask by default; allow_once/reject_once; late approval cannot restart cancelled actions |
| Questions | Negotiated form elicitation; explicit skip when unsupported, never invented user answers |
| Client files | Negotiated fs capabilities expose client_read_text_file/client_write_text_file |
| Client terminal | Negotiated create/wait/output; cancellation kills/releases; cleanup waits are bounded |
| MCP | stdio, HTTP and SSE servers passed with session setup; ephemeral per-session configuration |
| Content | Text, resource links, embedded text, images/binary attachments; binary data uses uploads |
| Compaction | `/compact` calls engine compaction |
| Not provided | Audio, additionalDirectories, interactive ACP login, persistent allow_always |

Image understanding also depends on the selected model. Maximum attachment size is 10 MiB, with up to 10 attachments per turn. Resource links are references, not automatic URL downloads. cwd must be an existing absolute local directory, and sessions remain bound to their workspace.

ask follows engine permission rules; it does not prompt for every workspace file operation. Client MCP tools are permission-gated. auto uses the engine automatic policy. full skips confirmation and OS confinement for commands; select it explicitly only when intended:

```bash
hugagent --mode local --output acp --permission-mode full
```

An isolated evaluation container can provide the outer sandbox for this mode. ageval should invoke the native command directly, without installing or calling the removed `evaluation/cli_acp` adapter.

## Lifecycle and failure handling

Client MCP credentials and policy are attached to an ephemeral lease. Heartbeats do not change policy. Each run freezes its admission policy so subsequent changes cannot elevate it. Cancellation, EOF and session closure cancel owned runs and release resources without stopping the shared engine.

If admission acknowledgement is lost after submission, the session is revoked and must be recreated. A failed backend cancellation leaves an unavailable lease tombstone to prevent fallback to account permissions; verify the run is stopped before restarting the local engine to recover the slot. Client terminal cleanup timeouts are reported on stderr; the client retains responsibility for final terminal cleanup.

Protocol errors omit backend logs and credentials. Logging redacts MCP URL query parameters and authorization values. Complete local onboarding before use. A successful protocol response does not prove a task was solved: verify the files and tests independently.

## Development and verification

Product modules: `packages/cli/src/acp/`, `packages/cli/src/modes/acp.ts`; backend leases and API: `integrations/agent_client/` (an installed module that registers its route and lease provider at startup); core reaches leases only through the `core/harness/registry/client_sessions.py` port.

```bash
(cd packages/cli && npm test)
PYTHONPATH=src/backend .venv/bin/pytest src/backend/tests/integrations/test_client_session*.py -q
```

`packages/cli/test/integration/acp-live.mjs` exercises a real model in an isolated container. `acp-installed.mjs` invokes the CLI installed on PATH, repairs an order-summary program and runs independent assertions. Both require explicit opt-in and separate scratch workspaces. Report protocol tests, live-model validation and independent WorkBuddy scores separately.
