"""the engine filesystem helpers — supports both sandbox and in-process callers.

``workdir()`` returns the directory the engine should read inputs from and
write outputs to. Resolution order, first match wins:

    1. **Thread-local override** (set via ``use_workdir(path)``).
       Used by the in-process MCP runner so concurrent tool calls each get
       their own temp workdir without racing on process-wide state.
    2. ``OFFICE_LIB_WORKDIR`` env var — explicit override (sandbox or test).
    3. ``/workspace`` exists and cwd is at-or-below it → cwd (script_runner).
    4. ``/workspace`` exists but cwd is elsewhere → ``/workspace`` (opensandbox).
    5. Else → cwd (dev/test default).
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# Thread-local override consulted before the env var. The mcp container's
# in-process runner serves concurrent tool calls on different threads via
# ``asyncio.to_thread``; ``OFFICE_LIB_WORKDIR`` and ``os.chdir`` are both
# process-wide, so neither can isolate concurrent calls. ``threading.local``
# does — each worker thread sees its own value.
_workdir_local = threading.local()


def _get_thread_local_workdir() -> Optional[str]:
    return getattr(_workdir_local, "value", None)


@contextmanager
def use_workdir(path: os.PathLike[str] | str) -> Iterator[Path]:
    """Pin the engine's workdir to ``path`` for the current thread.

    Restores the previous value on exit. Stacks safely.
    """
    resolved = Path(path).resolve()
    previous = getattr(_workdir_local, "value", None)
    _workdir_local.value = str(resolved)
    try:
        yield resolved
    finally:
        if previous is None:
            try:
                del _workdir_local.value
            except AttributeError:
                pass
        else:
            _workdir_local.value = previous


def workdir() -> Path:
    """Return the directory the engine should read inputs from + write outputs to."""
    local_override = _get_thread_local_workdir()
    if local_override:
        return Path(local_override).resolve()

    env_override = os.environ.get("OFFICE_LIB_WORKDIR")
    if env_override:
        return Path(env_override).resolve()

    workspace = Path("/workspace")
    if workspace.is_dir():
        cwd = Path.cwd().resolve()
        ws_resolved = workspace.resolve()
        try:
            cwd.relative_to(ws_resolved)
        except ValueError:
            # cwd is NOT under /workspace → opensandbox-style; use /workspace
            return ws_resolved
        else:
            # cwd IS /workspace itself or a subdir → script_runner-style; use cwd
            return cwd

    return Path.cwd().resolve()


def input_path(name: str) -> Path:
    """Resolve an input filename to its on-disk path.

    Raises FileNotFoundError if the file is not present.
    """
    p = workdir() / name
    if not p.is_file():
        raise FileNotFoundError(
            f"input file '{name}' not found in workdir {workdir()}; "
            "ensure it was passed via input_files_b64"
        )
    return p


def output_path(name: str) -> Path:
    """Resolve an output filename within the active workdir."""
    return workdir() / name
