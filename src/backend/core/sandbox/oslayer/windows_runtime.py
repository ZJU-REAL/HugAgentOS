"""Applying a Windows sandbox plan at the moment a process is spawned.

Windows confinement cannot be an argv prefix — a token has to be built and
handed to ``CreateProcessAsUser`` by whoever creates the process — so the
backend hands the spawning boundary a *plan*, already decided, and this module
applies it verbatim:

``writable_roots``
    Each an absolute path the command may write, with the existing paths inside
    it that must stay read-only.
``scratch_dir``
    The private directory standing in for the user's temp folder; absent when
    the policy grants no scratch space.
``state_dir``
    Where the roots currently carrying the sandbox's label are remembered.

A label is on-disk state that outlives the command, so a root stays labeled only
while a plan names it: at every spawn, roots labeled by earlier plans and named
by neither this plan nor a still-running command get their label taken back.
That keeps a one-off grant one-off, as the per-run token does on other
platforms.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from dataclasses import dataclass, field

# Environment variables Windows programs read to find their temp directory.
TEMP_ENV_KEYS = ("TEMP", "TMP")
ROOTS_FILENAME = "sandbox_labeled_roots.json"


def root_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


class LabeledRoots:
    """The roots currently carrying the sandbox's Low label, kept in ``state_dir``."""

    def __init__(self, state_dir: str) -> None:
        self._path = os.path.join(state_dir, ROOTS_FILENAME)

    def load(self) -> set[str]:
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return set()
        return {str(item) for item in data} if isinstance(data, list) else set()

    def save(self, roots: set[str]) -> None:
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        temporary = self._path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(sorted(roots), handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self._path)


def apply_labels(plan: dict) -> None:
    """Label this plan's roots and take the label back from roots no plan needs.

    Roots a still-running command was granted keep theirs until it exits.
    """
    from .win32.label import label_read_only, label_writable, remove_label

    roots = {root_key(root["path"]): root for root in plan.get("writable_roots") or []}
    store = LabeledRoots(plan["state_dir"])
    labeled = store.load()
    in_use = {key for key, count in process_state().IN_USE.items() if count > 0}
    stale = labeled - set(roots) - in_use
    for key in stale:
        remove_label(key)
    for root in roots.values():
        label_writable(root["path"])
        for read_only in root.get("read_only") or ():
            label_read_only(read_only)
    store.save((labeled - stale) | set(roots))


def child_environment(plan: dict, env: dict) -> dict:
    """The environment the command runs with, scratch space redirected.

    Returned as a new mapping rather than applied to this process: the caller is
    a long-lived server, and rewriting its own ``TEMP`` would leak one command's
    scratch directory into every other command it is running.
    """
    scratch = plan.get("scratch_dir")
    if not scratch:
        return dict(env)
    return {**env, **{key: scratch for key in TEMP_ENV_KEYS}}


def _sandbox_token():
    """The low-integrity token, built once: it takes nothing from the plan."""
    if process_state().TOKEN is None:
        from .win32 import ffi
        from .win32.token import create_low_integrity_token, open_current_process_token

        base_token = open_current_process_token()
        try:
            process_state().TOKEN = create_low_integrity_token(base_token)
        finally:
            ffi.CloseHandle(base_token)
    return process_state().TOKEN


def spawn_confined(
    plan: dict,
    command: list,
    *,
    cwd: str | None,
    env: dict,
    stdin: int,
    stdout: int,
    stderr: int,
):
    """Apply ``plan`` and start ``command`` under the low-integrity token.

    Returns a :class:`core.sandbox.oslayer.win32.spawn.TokenProcess`, which the
    caller must ``close()`` once it has read the exit code.
    """
    from .win32.spawn import spawn_with_token

    scratch = plan.get("scratch_dir")
    if scratch:
        os.makedirs(scratch, exist_ok=True)

    keys = [root_key(root["path"]) for root in plan.get("writable_roots") or []]

    def release() -> None:
        with process_state().LOCK:
            process_state().IN_USE.subtract(keys)

    with process_state().LOCK:
        apply_labels(plan)
        token = _sandbox_token()
        process_state().IN_USE.update(keys)
    try:
        return spawn_with_token(
            token,
            command,
            cwd=cwd,
            env=child_environment(plan, env),
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            on_close=release,
        )
    except BaseException:
        release()
        raise


__all__ = [
    "LabeledRoots",
    "ROOTS_FILENAME",
    "TEMP_ENV_KEYS",
    "apply_labels",
    "child_environment",
    "root_key",
    "spawn_confined",
]


@dataclass
class WindowsRuntimeState:
    LOCK: object = field(default_factory=lambda: threading.Lock())
    IN_USE: Counter[str] = field(default_factory=lambda: Counter())
    TOKEN: object = None


_STATE = WindowsRuntimeState()


def process_state() -> WindowsRuntimeState:
    return _STATE
