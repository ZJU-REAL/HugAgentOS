# Offline desktop local mode

The complete desktop package includes the platform-specific CE backend and a private Python runtime.
Starting local mode verifies and extracts the bundled resources without requiring Docker, system Python,
pip or a first-run dependency download. Thin packages support remote connections only.
See [Windows deployment](windows-deployment.md), [UOS deployment](uos1070-desktop.md) and
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
