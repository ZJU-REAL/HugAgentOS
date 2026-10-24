"""The user's own environment, for commands run in a folder the user chose (local mode).

A local project is the user's real folder, so its commands should find what the
user's terminal finds: Homebrew, pyenv, nvm, cargo, their git and ssh setup. A
desktop app started from the Dock or Start menu does not have that environment,
so on POSIX it is read once from a login shell, the way editors resolve it.

Two things stay as they are:

* Credentials never follow. Like Codex's default ``shell_environment_policy``,
  any variable whose name looks like a key, secret, token or password is left
  out, and so are this backend's own process variables.
* Skill scripts keep their interpreter. Bundled skills run ``python3 <skill>/…``
  and need the dependencies installed beside the backend, so a small shim in
  front of the user's PATH sends exactly those calls to ``$PY_BIN`` and every
  other ``python3`` to the user's own.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

_MARKER = "__HUGAGENT_LOGIN_ENV__"
_CAPTURE_TIMEOUT = 8
_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)
# Variables the backend sets for itself (see cli.apply_local_env) or a shell sets per process.
_INTERNAL_PREFIXES = ("HUGAGENT_", "JX_", "SANDBOX_", "MCP_", "BACKEND_", "SCRIPT_RUNNER_", "SITE_", "MILVUS_", "MEM0_")
_INTERNAL_NAMES = frozenset({
    "DATABASE_URL", "REDIS_URL", "DEPLOY_PROFILE", "AUTH_MODE", "SESSION_STORE", "SSO_LOGIN_MODE",
    "LOCAL_AUTH_ENABLED", "STORAGE_TYPE", "STORAGE_PATH", "LOG_FILE_PATH", "FRONTEND_DIST_DIR",
    "BRAND_PRODUCT_NAME", "PORT", "NO_PROXY", "no_proxy", "PYTHONPATH", "VIRTUAL_ENV",
    "_", "PWD", "OLDPWD", "SHLVL", "PS1", "PS2", "PROMPT", "TERM_SESSION_ID",
})
# Managed values the runner relies on; the user's copy never replaces them.
_MANAGED = ("PY_BIN", "NODE_PATH", "PLAYWRIGHT_BROWSERS_PATH", "JX_FONT_DIR", "PDF_SKILL_DIR",
            "OFFICECLI_SKIP_UPDATE", "OFFICECLI_RESIDENT_FLUSH", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE")
_MANAGED_PREFIXES = ("SITE_", "SCRIPT_RUNNER_")

SHIM_DIR_ENV = "HUGAGENT_SHIM_DIR"
SHIM_ROOTS_ENV = "HUGAGENT_SHIM_SKILL_ROOTS"
_SHIM = r"""#!/bin/sh
# HugAgentOS: a skill's own script runs on the bundled interpreter, where its
# dependencies are installed; every other call is the user's own interpreter.
name=${0##*/}
for arg in "$@"; do
  case "$arg" in -*) continue ;; esac
  saved=$IFS; IFS=:
  for root in $HUGAGENT_SHIM_SKILL_ROOTS; do
    case "$arg" in "$root"/*) IFS=$saved; exec "$PY_BIN" "$@" ;; esac
  done
  IFS=$saved
  break
done
rest=
saved=$IFS; IFS=:
for dir in $PATH; do [ "$dir" = "$HUGAGENT_SHIM_DIR" ] || rest=${rest:+$rest:}$dir; done
IFS=$saved
PATH=$rest; export PATH
found=$(command -v "$name" 2>/dev/null)
# macOS ships a /usr/bin/python3 stub that only offers to install the developer tools.
if [ "$found" = "/usr/bin/$name" ] && [ "$(uname)" = Darwin ] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
  found=
fi
[ -n "$found" ] && exec "$found" "$@"
exec "$PY_BIN" "$@"
"""


def is_user_folder(cwd: str | os.PathLike, workspace_root: str) -> bool:
    """True when ``cwd`` is a folder of the user's, not the runner's own workspace."""
    try:
        return not Path(cwd).resolve().is_relative_to(Path(workspace_root).resolve())
    except OSError:
        return False


def _keep(name: str) -> bool:
    return not (
        name in _INTERNAL_NAMES
        or name.startswith(_INTERNAL_PREFIXES)
        or _SECRET_NAME.search(name)
    )


def _filtered(environ: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in environ.items() if _keep(name)}


def _parse_login_output(output: bytes) -> dict[str, str]:
    """Entries printed by ``env -0`` after the marker; anything a profile echoed before it is ignored."""
    text = output.decode("utf-8", errors="replace")
    if _MARKER not in text:
        return {}
    entries: dict[str, str] = {}
    for item in text.split(_MARKER, 1)[1].split("\0"):
        name, sep, value = item.partition("=")
        if sep and name and "\n" not in name:
            entries[name] = value
    return entries


@lru_cache(maxsize=1)
def login_environment() -> dict[str, str]:
    """The user's login-shell environment, credentials and backend internals removed.

    Read once per runner. Falls back to this process's own environment when there
    is no login shell to ask (Windows) or it does not answer in time.
    """
    captured: dict[str, str] = {}
    if os.name != "nt":
        shell = os.environ.get("SHELL") or "/bin/sh"
        try:
            import pwd

            shell = pwd.getpwuid(os.getuid()).pw_shell or shell
        except (ImportError, KeyError):
            pass
        seed = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME") if key in os.environ}
        seed.update(SHELL=shell, TERM="dumb", PATH="/usr/bin:/bin:/usr/sbin:/sbin")
        try:
            done = subprocess.run(
                [shell, "-l", "-i", "-c", f"printf %s {_MARKER}; command env -0"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=seed, timeout=_CAPTURE_TIMEOUT, start_new_session=True,
            )
            captured = _parse_login_output(done.stdout)
        except (OSError, subprocess.SubprocessError):
            captured = {}
    return _filtered(captured or os.environ)


@lru_cache(maxsize=4)
def _shim_directory(workspace_root: str) -> str:
    """Write the python shims once per runner, where confined commands can read them.

    Only the runner's own workspace: a shared temp directory could be created
    first by another local user. Without it the caller keeps the clean
    environment.
    """
    return _write_shims(Path(workspace_root) / ".runtime" / "bin")


def _write_shims(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("python3", "python"):
        target = directory / name
        if not target.is_file() or target.read_text(encoding="utf-8") != _SHIM:
            temporary = target.with_name(f".{name}.{os.getpid()}.tmp")
            temporary.write_text(_SHIM, encoding="utf-8")
            temporary.chmod(0o755)
            os.replace(temporary, target)
    return str(directory)


def _join_path(*groups: Iterable[str]) -> str:
    seen: dict[str, None] = {}
    for group in groups:
        for entry in group:
            if entry:
                seen.setdefault(entry, None)
    return os.pathsep.join(seen)


def project_environment(managed: Mapping[str, str], workspace_root: str, skill_roots: Iterable[str]) -> dict[str, str]:
    """Environment for a command in a user's folder: theirs, plus what the runner needs.

    ``managed`` is the runner's own clean environment. Its tool directories go
    after the user's PATH, so the user's ``python3``, ``node`` or ``git`` win.
    The result is laid over ``managed``, so a managed variable the user does not
    set (a scratch cache directory, thread limits) stays as the runner had it.
    """
    user = login_environment()
    env = dict(user)
    for name, value in managed.items():
        if name in _MANAGED or name.startswith(_MANAGED_PREFIXES) or name not in env:
            env[name] = value
    if managed.get("NODE_PATH") and user.get("NODE_PATH"):
        env["NODE_PATH"] = _join_path(managed["NODE_PATH"].split(os.pathsep), user["NODE_PATH"].split(os.pathsep))
    user_path = user.get("PATH", "").split(os.pathsep)
    managed_path = managed.get("PATH", "").split(os.pathsep)
    env["PY_BIN"] = managed.get("PY_BIN") or sys.executable
    if os.name == "nt":
        env["PATH"] = _join_path(user_path, managed_path)
        return env
    shim = _shim_directory(workspace_root)
    env[SHIM_DIR_ENV] = shim
    # A skill may be named through its workspace link or the store it points to.
    env[SHIM_ROOTS_ENV] = os.pathsep.join(
        dict.fromkeys(form for root in skill_roots if root for form in (root.rstrip("/"), os.path.realpath(root)))
    )
    env["PATH"] = _join_path([shim], user_path, managed_path)
    return env
