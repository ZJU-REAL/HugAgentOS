# Offline desktop local mode

The complete desktop package includes the platform-specific CE backend and a private Python runtime.
Starting local mode verifies and extracts the bundled resources without requiring Docker, system Python,
pip or a first-run dependency download. Thin packages support remote connections only.
See [Windows deployment](windows-deployment.md) and
the [deployment overview](README.md) for installation options.

## Persistent data

Windows stores application data under the desktop application's local-server data directory.
macOS and Linux use `~/.hugagent`. Application/runtime versions are stored separately from persistent data.
Desktop upgrades retain conversation files; failed runtime activation rolls back to the previous version.

## Conversation workspaces and skill paths

Each conversation keeps its default workspace at `<data directory>/workspace/.sessions/<conversation hash>/`.
Repeated runs reuse that directory; idle time does not delete it. Other conversations use separate directories.
File tools and Bash resolve relative paths against the same working directory. Absolute paths address native
files directly, without container aliases. Bound local projects expose their actual folder in the project context.

Concurrent commands use separate temporary scripts while retaining the conversation working directory, and
remove only their own scripts when finished. Skill listings expose the real instruction path for the version
selected by the run. Skills do not prescribe deployment directories: scripts run through quoted real paths
and leave outputs in the working directory. Windows drive letters, Unicode and spaces are handled as command
arguments. Container sandboxes retain separate mount and My Space policies.

## Verification

Validate installation and upgrades on each supported native platform. Workspace regression checks cover
parallel execution, preservation of user files, relative-path artifact transfer and relocated skill scripts.
Linux unit tests do not replace native Windows or macOS package acceptance.

## Local file delivery

Local mode delivers files from the current conversation workspace or bound project with
`pin_to_workspace(file_paths=[...])`. It does not expose `sandbox_get_artifact` to the model.
Project files remain live references; conversation outputs are automatically saved as attachments.
Local read permissions still apply. Prefer real absolute paths: delivery resolves relative paths
against the bound project, or the conversation workspace when no local project is bound.
Cloud mode retains export followed by file-ID delivery. All-failed calls return `ok: false`;
partial failures include separate successful and failed lists.

## macOS signing and Keychain authorization

A fixed self-signed certificate supports stable upgrade identity without Apple enrollment; Developer ID with notarization is also supported.
Reuse the same certificate and application identifier to preserve Keychain authorization across rebuilds.
See [macOS signing and upgrades](macos-signing.md) for build checks, certificate setup and existing-user migration.

## Workflow job callbacks

In local mode, `run_job` reports progress to `http://127.0.0.1:<local backend port>`.
The port follows the desktop build configuration. Docker service names and host mappings are unnecessary.
Before starting a job, the script runner probes the backend's `/health` endpoint and refuses to start if it is unreachable.

`JOB_CALLBACK_URL` can explicitly override the callback base URL. Set it only when needed and point it at the current local backend.
Installed clients need a complete desktop package containing this fix. Updating the cloud backend alone does not update the local service.

Health probes and job progress callbacks connect directly to loopback backends, bypassing system proxies.
Non-loopback callback URLs and the user's own HTTP code retain their existing proxy behavior.
Probe execution failures report the exit code and a runtime diagnostic instead of claiming that the backend is unreachable.

For native acceptance, use the desktop's private Python to run `src/backend/tests/integration/job_local_smoke.py --output <new absolute directory>`.
It uses isolated SQLite data, workspaces and loopback ports to verify real `run_job` startup, partial progress,
completion callbacks, the disk receipt, a system proxy simulation, expired token rejection and interpreter failure classification.
It does not package or update the installed client.

Local project and session deliveries reference original files without creating delivery copies. Reopening reads the current content; Canvas saves edit the original. Moving or deleting the file invalidates its historical card. Click a card to preview. Local cards retain native default opening, associated applications and Reveal in folder, with no download action. Cloud cards show a labelled Download button. One file fills a row; multiple files use two columns and collapse after four, with one column in narrow windows. Cloud files retain download delivery.
