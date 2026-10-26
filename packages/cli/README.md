# hugagent

HugAgentOS in your terminal. The agent, tools, sandbox, compaction, skills and memory all run in the
HugAgentOS backend; this package is the client: a pi-style TUI, a one-shot `-p` mode, a JSONL event
stream and a JSON-lines embedding protocol. Design: [DESIGN.md](DESIGN.md) (§8 is the contract).

## Install

Local mode needs the Python backend's `hugagent-server` command. From the repository root:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install --no-deps -e .
hugagent-server onboard --no-serve        # once: admin account and main model

(cd packages/chat-sdk && npm install && npm run build)
(cd packages/cli && npm install && npm run build && npm install -g .)

cd ~/code/my-project && hugagent
```

`npm install -g .` links the checkout, so `npm run build` is enough after a change. To run from the checkout
without a global install, use `node packages/cli/dist/main.js`. If `hugagent-server` is not on `PATH`, point the
client at the repo with `HUGAGENT_REPO=<checkout>` and `HUGAGENT_PYTHON=<checkout>/.venv/bin/python`.

## Where the agent runs: `--mode`

| Mode | What it talks to | Local files |
|---|---|---|
| `local` (default) | the shared headless engine on this machine (`hugagent-server engine`) | the current directory, bound as a local project |
| `cloud` | a remote HugAgentOS (`hugagent login`) | none: attach files instead |
| `device:<id>` | another paired computer, through the cloud's session-manager queue | that computer's |

The default is `--mode`, then `defaultMode` in `$HUGAGENT_HOME/cli.json`, then `local`.

### local

1. Reads `$HUGAGENT_HOME/engine.json` (default `~/.hugagent`); if its pid is alive and `GET /health` answers 200, attaches.
2. Otherwise starts the engine detached, logging to `$HUGAGENT_HOME/logs/engine.log`, and waits up to 120 s for the ready file.
   The command is the first of: `HUGAGENT_ENGINE_CMD` (JSON array, e.g. `["python3","/repo/src/backend/cli.py"]`),
   `hugagent-server` on `PATH`, `[${HUGAGENT_PYTHON:-python3}, "$HUGAGENT_REPO/src/backend/cli.py"]`.
3. Binds the current directory with `POST /v1/projects/local/resolve`; new sessions go into that project and
   `-c` / `-r` only see its sessions.

The engine is shared and idles out by itself; `hugagent` never stops it. A first start takes about 10 s,
later ones about 6 s, an attach about 0.1 s. A machine that was never set up exits 3: run `hugagent onboard --no-serve`.

### cloud and device

```bash
hugagent login --server https://agent.example.com/api   # prompts for a personal API key, verifies it
hugagent --mode cloud
hugagent --mode device:<id> -p "what is running?"
hugagent logout
```

Credentials live in `$HUGAGENT_HOME/cli.json` (mode 0600); `HUGAGENT_SERVER` / `HUGAGENT_API_KEY` override it,
and the saved key is only sent to the server it was saved for. Plain `http://` is refused except for loopback
or with `HUGAGENT_ALLOW_HTTP=1`. The server URL is the API root: include `/api` behind the default nginx gateway.

Device mode has no live stream: a turn shows `run_started`, then the final answer once the run settles. It cannot
fork, compact, approve, answer questions, pick models or change permission modes.

## Non-interactive

```bash
hugagent -p "list the files here"          # answer on stdout, tool lines and notes on stderr
git diff | hugagent -p "review this"       # no prompt: read stdin (or pass -p -)
hugagent -p "fix the test" --output json   # every SSE event as a line, then a cli_result line
hugagent -c -p "and now the docs"          # continue the latest session of this directory
hugagent -r chat_59b5… -p "go on"          # a given session
```

The last JSON line is `{"type":"cli_result","status":"completed|failed|cancelled","chat_id","run_id"}`
(plus `error` when it did not complete). Events are passed through unchanged; see
`.claude/skills/hugagent-frontend-dev/references/sse-events.md`.

Approvals: a `file_confirm` is denied unless `--auto-approve`; every `user_question` is skipped. Ctrl+C cancels
the run on the server and exits 130 (a second Ctrl+C exits at once).

Exit codes: 0 completed, 1 failed, 2 usage error, 3 setup required (`onboard` or `login`), 130 interrupted.

## `--output rpc`

One JSON command per stdin line, replies and events on stdout. Commands run concurrently, so `cancel`, `steer`
and `confirm` work while a `send` streams.

```jsonc
→ {"id":1,"method":"newSession","params":{"title":"from my editor"}}
← {"id":1,"result":{"chatId":"chat_…","title":"from my editor","projectId":"prj_…"}}
→ {"id":2,"method":"send","params":{"chatId":"chat_…","message":"hi"}}
← {"event":{"type":"run_started","run_id":"…"},"request":2}
← {"event":{"type":"content","delta":"Hello"},"request":2}
← {"id":2,"result":{"status":"completed","chatId":"chat_…","runId":"…"}}
```

Methods mirror `src/engine/engine.ts`: `info`, `connect`, `close`, `newSession {title?}`, `listSessions {limit?}`,
`history {chatId, limit?}`, `fork {chatId, messageId?}`, `send {chatId, message, model?, attachments?}`,
`resume {runId, fromOffset?}`, `activeRun {chatId}`, `cancel {runId}`, `steer {runId, message, mode?}`,
`confirm {chatId, confirmId, decision, optionId?}`, `answer {chatId, requestId, answers}`, `compact {chatId}`,
`listModels`, `getPermissionMode`, `setPermissionMode {mode}`. Errors are
`{"id","error":{"message","status"?,"code"?,"exitCode"?}}`. No approval policy applies: the client answers
`file_confirm` and `user_question` itself. stdin EOF waits for running commands, then exits 0.

## Server commands

`hugagent serve|onboard|doctor …` runs `hugagent-server` (found the same way as the engine) with the terminal and
passes its exit code on.

## Layout

```
src/main.ts            arguments, login/logout, server forwarding, mode dispatch
src/config.ts          $HUGAGENT_HOME/cli.json, server URL policy, credentials
src/engine/engine.ts   the Engine interface the UI is built against
src/engine/http.ts     authenticated HTTP over @hugagent/chat-sdk, envelope + error handling
src/engine/base.ts     shared HTTP engine (reconnects a dropped stream from its last offset)
src/engine/local.ts    engine discovery / spawn, cwd → local project
src/engine/remote.ts   cloud and device:<id>
src/engine/headless.ts one non-interactive turn: session choice, approval policy, Ctrl+C
src/engine/index.ts    EngineFactory
src/modes/             interactive (TUI), print, json, rpc
test/                  node --test against a fake HugAgentOS (node:http + SSE)
```

`npm test` builds and runs the tests.

## Native ACP v1

Launch `hugagent --mode local --output acp` from an ACP client. Permissions default to ask;
`--permission-mode ask|auto|full` sets the initial session policy. Full mode disables OS confinement.
See [ACP integration](../../document/en/development/cli-acp.md) and
[中文说明](../../document/zh-CN/development/cli-acp.md) for capabilities, lifecycle and tests.
