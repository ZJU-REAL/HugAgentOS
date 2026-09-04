"""Unified sandbox driver interface and data contracts.

Field names/types are aligned one-to-one with
``services/script_runner_service/server.py:ExecuteRequest/ExecuteResponse``,
so that ScriptRunnerProvider is a pure pass-through and OpenSandboxProvider
only performs semantic alignment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol


@dataclass
class SandboxFile:
    """Artifact file collected after a sandbox execution."""

    name: str
    size: int
    content_b64: str
    mime_type: str


@dataclass
class ExecuteRequest:
    """Unified execute request.

    Note: ``_args`` (list) inside ``params`` is interpreted as command-line
    arguments appended after the interpreter invocation; the remaining keys
    are serialized as JSON and written to stdin.

    ``user_id``: optional. Providers use it to expose the caller's files under
    ``STORAGE_PATH/myspace_cache/{user_id}/`` into the sandbox at
    ``/workspace/myspace/{user_id}/``, so that files staged via
    ``stage_files`` are visible in subsequent execute calls.

    ``session_id``: the conversation-level sandbox identity. Every provider
    uses it as the sole isolation boundary: main agents, child agents and
    batch work in the same session workspace; different sessions do not.
    Container providers additionally reuse their language/kernel state.

    ``expected_output_files``: optional. The caller declares in advance the
    list of output file names it expects to retrieve from the sandbox (file
    names relative to /workspace, no directories). Providers use it for
    precise retrieval — this matters especially because the opensandbox
    SDK's ``files.search`` is unavailable in some versions, and artifact
    collection relying on list-and-diff would return empty. With explicit
    expected file names declared, the provider takes the precise
    ``get_file_info`` + ``read_bytes`` retrieval path, bypassing the list
    failure. Missing files do not raise (treated as stale or not produced
    by the script); the resulting ExecuteResult.files only contains files
    that were successfully retrieved. The script_runner provider is also
    compatible with this field (still uses the original work_dir scan; the
    extra field has no side effects).
    """

    script_content: str
    script_name: str
    language: str = "python"
    params: dict = field(default_factory=dict)
    timeout: int = 60
    resource_files: Optional[dict[str, str]] = None
    input_files: Optional[dict[str, str]] = None
    input_files_b64: Optional[dict[str, str]] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    expected_output_files: Optional[list[str]] = None


@dataclass
class ExecuteResult:
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: int
    files: list[SandboxFile] = field(default_factory=list)


@dataclass
class StageFile:
    """Input file staged into myspace."""

    name: str
    content_b64: str


@dataclass
class StagedFile:
    """Location of a staged file, referenced by scripts via absolute path."""

    name: str
    path: str


class SandboxAdminNotSupported(Exception):
    """The current provider does not support this read-only admin capability
    (e.g. ScriptRunner cannot enumerate instances).

    The security admin console uses this to gray out the corresponding
    columns/buttons; it must not be treated as a 500 error.
    """


@dataclass
class SandboxAdminCapabilities:
    """How far a provider supports the read-only "Security Admin → Sandbox
    Management" view.

    The UI trims the interface based on these flags: unsupported
    capabilities are hidden/grayed out directly — never pretend they work.
    ``can_kill`` is a write operation, permanently ``False`` for this phase,
    reserved for a later write-operation stage.
    """

    provider: str
    can_list: bool = False  # can enumerate running instances
    can_inspect: bool = False  # can view single-instance detail (files / recent output)
    can_pool_stats: bool = False  # can provide connection-pool statistics
    can_snapshots: bool = False  # has a snapshot system (chat_sandbox_snapshots)
    can_kill: bool = False  # write operation, not implemented this phase


@dataclass
class SandboxInfo:
    """Read-only snapshot of a running sandbox instance (for display in the
    security admin console).

    Note: ``idle_seconds`` is a relative value computed from
    ``time.monotonic()``, not wall-clock time — providers track activity
    internally with a monotonic clock, so absolute creation/activity
    timestamps cannot be reliably reconstructed.
    """

    sandbox_id: str
    session_id: Optional[str] = None  # bound chat_id (if any)
    user_id: Optional[str] = None
    pool_kind: Optional[str] = None  # jupyter / light / user / general / warm
    state: str = "active"  # active | idle | stale
    idle_seconds: Optional[float] = None  # now - last_active (monotonic clock delta)
    owner_tag: Optional[str] = None
    backend_managed: bool = True  # whether in the current process registry
    # Ownership classification for detached instances (backend_managed=False),
    # derived from owner_tag:
    # self-orphan (our own orphan) | external (another client) | untagged (no owner tag).
    ownership: Optional[str] = None
    extra: dict = field(default_factory=dict)


class SandboxProvider(Protocol):
    """Sandbox driver interface. Each provider implementation must declare
    ``name`` for logging and health reporting."""

    name: str

    # 沙箱里的 ``/workspace/myspace/{uid}`` 是否就是后端的 myspace_cache 目录本身
    # （bind mount），而不是一份需要来回搬运的副本。True 时沙箱里的写入立刻落在后端磁盘
    # 上，对账可以直接读本地目录、不必再走沙箱取文件；也意味着"沙箱副本和镜像缓存一致"
    # 不再等于"已登记进我的空间"，判定必须以 artifact 记录为准。
    myspace_mirror_live: bool = False

    async def execute(self, req: ExecuteRequest) -> ExecuteResult: ...

    async def stage_files(self, user_id: str, files: list[StageFile]) -> list[StagedFile]: ...

    async def put_file(
        self,
        session_id: Optional[str],
        path: str,
        content: bytes,
        user_id: Optional[str] = None,
    ) -> None:
        """Write bytes to the given path in the sandbox (parent directories
        are created automatically if needed).

        ``session_id``: binds the operation to the corresponding conversation
        workspace; every provider must preserve visibility for later calls
        carrying the same id and isolate calls carrying a different id.
        ``user_id``: if this call would create a **new** session, used to
        bind the sandbox to that user (mounting their myspace/credential
        volumes). Omitting it lets a file operation that precedes execute
        create a credential-less session; subsequent bash reuse then cannot
        access the user's credentials (CLIs like Lark/Feishu report
        not_configured).
        """
        ...

    async def get_file(
        self,
        session_id: Optional[str],
        path: str,
        user_id: Optional[str] = None,
    ) -> bytes:
        """Read file bytes from the sandbox. Raises SandboxError if the file
        does not exist or reading fails.

        ``user_id``: see ``put_file`` — used to bind the user's credential
        volume when a new session is created.
        """
        ...

    async def get_file_to_path(
        self,
        session_id: Optional[str],
        path: str,
        destination: Path,
        *,
        max_bytes: int,
        user_id: Optional[str] = None,
    ) -> int:
        """Stream a sandbox file into a backend-local path.

        Implementations must enforce ``max_bytes`` while streaming, delete a
        partial destination on failure, and return the number of bytes written.
        This path is used for downloadable artifacts so large binaries never
        need to be represented as Base64 tool payloads.
        """
        ...

    async def current_sandbox_id(self, session_id: Optional[str]) -> Optional[str]:
        """Return the identity of the underlying sandbox currently bound to
        ``session_id``.

        Purpose: callers (e.g. the skill materializer in register_bash) need
        to know "this is now a different sandbox" when the sandbox is
        reclaimed / rebuilt, so they can decide whether to re-sync files.

        Conventions:
        - opensandbox-like: return a container-level UUID such as
          ``sess.sandbox.id``; after the session is destroyed by
          ``_destroy_session`` and a new sandbox is created, the id is
          guaranteed to change.
        - script_runner-like sidecars: return a stable identity derived from
          the logical session workspace.
        - If ``session_id`` is not bound to any sandbox (not yet created),
          return None; callers should treat this as "sandbox identity not
          yet determined" and query again after a real operation has created
          the sandbox.

        Implementations must not create sessions/sandboxes in this method —
        pure query, zero side effects.
        """
        ...

    async def close_session(self, session_id: Optional[str]) -> None:
        """Explicitly destroy a persistent-session sandbox (for
        conversation lifecycle cleanup or an explicit rebuild).

        An unknown session_id is a no-op. Never raises.
        """
        ...

    async def touch_session(self, session_id: str) -> bool:
        """Mark a live session as active without executing anything in it.

        Long-running workflows call this on a timer (see
        ``core/sandbox/keepalive.py``) so the idle reaper does not destroy
        their session during long model-streaming phases with no sandbox
        calls. Returns False when the session does not exist (yet); providers
        with no session lifecycle implement it as a no-op returning False.
        Never raises.
        """
        ...

    async def health(self) -> bool: ...

    # ── Read-only admin interface (for the security admin console; all pure
    # queries with zero side effects) ──────────────
    # Unsupported capabilities should raise SandboxAdminNotSupported, and the
    # caller degrades the display accordingly.

    def admin_capabilities(self) -> "SandboxAdminCapabilities":
        """Declare how far this provider supports the read-only sandbox
        management view."""
        ...

    async def admin_list_sandboxes(self, include_server: bool = False) -> list["SandboxInfo"]:
        """Enumerate running sandbox instances.

        By default returns only instances in the current process registry
        (``backend_managed=True``). With ``include_server=True``, providers
        that support server-side enumeration **additionally** list detached
        instances that are still alive on the server but untracked by this
        process (``backend_managed=False``, with ``ownership`` labeling the
        attribution), used by the security admin console to discover leaks;
        providers that do not support it ignore the parameter and still
        return only registry instances.
        """
        ...

    async def admin_get_sandbox(self, sandbox_id: str) -> Optional["SandboxInfo"]:
        """Fetch single-instance detail by sandbox_id (``extra`` may carry a
        file listing / recent stdout)."""
        ...

    def admin_pool_stats(self) -> dict: ...
