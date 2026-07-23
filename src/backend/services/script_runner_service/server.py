"""
Skill script execution sidecar service.

Receives HTTP requests from the backend and executes predefined scripts in a
restricted subprocess. This service runs in a separate container with no
database/Redis/API-key access.
"""

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import resource
except ImportError:  # Windows does not provide the POSIX resource module.
    resource = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("script-runner")

app = FastAPI(title="HugAgentOS Script Runner", docs_url=None, redoc_url=None)

# ── Configuration ──
MAX_TIMEOUT = int(os.getenv("SCRIPT_MAX_TIMEOUT", "120"))
DEFAULT_TIMEOUT = int(os.getenv("SCRIPT_DEFAULT_TIMEOUT", "30"))
MAX_MEMORY_MB = int(os.getenv("SCRIPT_MAX_MEMORY_MB", "256"))
# Workspace root. In the Docker sidecar this stays the container-absolute
# ``/workspace`` (a mounted tmpfs). In the no-Docker local profile the runner is
# a plain host subprocess, so the CLI points it at a real host dir such as
# ``~/.hugagent/workspace`` via ``SCRIPT_RUNNER_WORKSPACE``. Everything under here
# is created on first use; ``_validate_workspace_path`` confines writes to it.
WORKSPACE_ROOT = os.getenv("SCRIPT_RUNNER_WORKSPACE", "/workspace")
MAX_OUTPUT_BYTES = 1024 * 1024  # 1MB
MAX_SCRIPT_SIZE = 512 * 1024  # 512KB

# Local-profile /workspace→real-root rewrite for executed script text. Compiled
# once here (invariant: WORKSPACE_ROOT is read from env at import). None in Docker,
# where the roots are equal and no rewrite is needed. Match /workspace only at a
# path boundary so an unrelated substring like /workspaces is left alone.
_WS_PATH_RE = re.compile(r'(?<![A-Za-z0-9_.\\/-])/workspace(?=/|$|["\'\s:;)&|])')


def _rewrite_workspace_refs(value: str, workspace_root: str = WORKSPACE_ROOT) -> str:
    """Map canonical workspace references without treating ``\\`` as regex escapes."""
    if not isinstance(value, str) or workspace_root == "/workspace":
        return value
    replacement = workspace_root.rstrip("/\\")
    return _WS_PATH_RE.sub(lambda _match: replacement, value)


def _bash_quote_state(value: str, end: int) -> Optional[str]:
    """Return the shell quote containing ``value[end]`` (single/double/None).

    The local desktop workspace commonly lives below macOS ``Application
    Support``.  A blind ``/workspace`` replacement therefore turns a valid
    unquoted command into several shell words.  We only need enough shell
    awareness to preserve existing quotes and safely quote unquoted path
    prefixes; Bash remains responsible for parsing the full script.
    """
    state: Optional[str] = None
    escaped = False
    for char in value[:end]:
        if state == "single":
            if char == "'":
                state = None
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
        elif state == "double":
            if char == '"':
                state = None
        elif char == "'":
            state = "single"
        elif char == '"':
            state = "double"
    return state


def _rewrite_bash_workspace_refs(value: str, workspace_root: str) -> str:
    """Map canonical workspace paths without breaking paths that contain spaces."""
    if not isinstance(value, str) or workspace_root == "/workspace":
        return value
    replacement = workspace_root.rstrip("/\\")

    def _replace(match: re.Match[str]) -> str:
        quote_state = _bash_quote_state(value, match.start())
        if quote_state == "single":
            return replacement.replace("'", "'\"'\"'")
        if quote_state == "double":
            return (
                replacement.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("$", "\\$")
                .replace("`", "\\`")
            )
        # Quoting only the rewritten prefix is valid shell concatenation:
        # '/Users/.../workspace'/site resolves as one path while the suffix
        # remains visible to the boundary-matching regex.
        return shlex.quote(replacement)

    return _WS_PATH_RE.sub(_replace, value)


def _execution_workspace_root(
    language: str,
    workspace_root: str = WORKSPACE_ROOT,
    platform: str = os.name,
) -> str:
    """Return the path syntax understood by the selected host interpreter."""
    if language != "bash" or platform != "nt":
        return workspace_root
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", workspace_root)
    if not match:
        return workspace_root.replace("\\", "/")
    drive, rest = match.groups()
    return f"/{drive.lower()}/{rest.replace(chr(92), '/')}"


def _rewrite_execution_paths(value: str, language: str) -> str:
    target_root = _execution_workspace_root(language)
    if target_root != WORKSPACE_ROOT:
        # File tools may already have expanded /workspace to the native root.
        value = value.replace(WORKSPACE_ROOT, target_root)
    if language == "bash":
        return _rewrite_bash_workspace_refs(value, target_root)
    return _rewrite_workspace_refs(value, target_root)


def _resolve_bash_executable() -> Optional[str]:
    """Find a native Bash, excluding Windows' WSL launcher stubs."""
    configured = os.getenv("SCRIPT_RUNNER_BASH", "").strip()
    candidates = [configured, shutil.which("bash") or ""]
    if os.name == "nt":
        for root in (
            os.getenv("ProgramFiles", ""),
            os.getenv("ProgramFiles(x86)", ""),
            str(Path(os.getenv("LOCALAPPDATA", "")) / "Programs"),
        ):
            if root:
                candidates.append(str(Path(root) / "Git" / "bin" / "bash.exe"))

    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        normalized = candidate.replace("/", "\\").casefold()
        if os.name == "nt" and (
            "\\windows\\system32\\bash.exe" in normalized
            or "\\microsoft\\windowsapps\\bash.exe" in normalized
        ):
            continue
        return candidate
    return None


_BASH_EXECUTABLE = _resolve_bash_executable()

INTERPRETERS = {
    # Use the running venv on local Windows/macOS/Linux installations.  A bare
    # ``python3`` is not installed on a standard Windows machine.
    "python": [sys.executable, "-u"],
    "bash": [_BASH_EXECUTABLE or "hugagent-git-bash-not-installed"],
    "javascript": [shutil.which("node") or "node"],
}

# ── Generated-file capture ──
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB per file
MAX_TOTAL_FILE_SIZE = 20 * 1024 * 1024  # 20MB total
MAX_FILE_COUNT = 20
ALLOWED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".csv",
    ".xlsx",
    ".xls",
    ".json",
    ".txt",
    ".pdf",
    ".html",
    ".htm",
    ".docx",
    ".pptx",
    ".md",
}

# Clean environment variables — leak no sensitive information
_TEMP_ROOT = tempfile.gettempdir()
SAFE_ENV = {
    "PATH": "" if os.name == "nt" else "/usr/local/bin:/usr/bin:/bin",
    "HOME": os.getenv("USERPROFILE", _TEMP_ROOT) if os.name == "nt" else "/tmp",
    "TMPDIR": _TEMP_ROOT,
    "TEMP": _TEMP_ROOT,
    "TMP": _TEMP_ROOT,
    "XDG_CACHE_HOME": str(Path(_TEMP_ROOT) / ".cache"),
    "LANG": "en_US.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "MPLBACKEND": "Agg",  # matplotlib non-interactive backend
    "OPENBLAS_NUM_THREADS": "1",  # prevent OpenBLAS from allocating lots of thread memory
    "OMP_NUM_THREADS": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",  # disable dotnet telemetry
    "DOTNET_NOLOGO": "1",  # suppress dotnet startup banner
    "DOTNET_EnableDiagnostics": "0",  # stop dotnet from creating diagnostic pipes/core dump files
}
if os.name != "nt":
    SAFE_ENV.update(
        {
            "FONTCONFIG_PATH": "/etc/fonts",
            "FONTCONFIG_FILE": "/etc/fonts/fonts.conf",
        }
    )
else:
    # These variables are required by CreateProcess and common Windows CLIs.
    for _key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
        _val = os.getenv(_key)
        if _val:
            SAFE_ENV[_key] = _val
for _key in ("NODE_PATH", "PLAYWRIGHT_BROWSERS_PATH", "JX_FONT_DIR"):
    _val = os.getenv(_key)
    if _val:
        SAFE_ENV[_key] = _val

_LOCAL_SKILL_CLI_IDS = ("word-editing", "excel-editing", "ppt-design", "pdf-editing")


def _local_safe_path_entries() -> list[str]:
    """Return trusted executable directories for the no-Docker runner.

    The quick installer runs the backend from ``~/.hugagent/venv`` while the
    subprocess sandbox intentionally starts from a clean PATH. Include that
    venv explicitly so skill shims use the same Python dependencies as the
    server, then expose each materialized built-in Office CLI without copying
    executables into a system directory.
    """
    entries = [os.path.dirname(sys.executable)]
    skills_root = os.getenv("SANDBOX_SKILLS_DIR", "").strip()
    if skills_root:
        entries.extend(
            str(Path(skills_root) / skill_id / "scripts") for skill_id in _LOCAL_SKILL_CLI_IDS
        )

    if os.name == "nt":
        system_root = os.getenv("SYSTEMROOT") or os.getenv("WINDIR")
        if system_root:
            entries.extend(
                [
                    str(Path(system_root)),
                    str(Path(system_root) / "System32"),
                    str(Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0"),
                ]
            )
        if _BASH_EXECUTABLE:
            git_bin = Path(_BASH_EXECUTABLE).parent
            entries.extend(
                [
                    str(git_bin),
                    str(git_bin.parent / "usr" / "bin"),
                    str(git_bin.parent / "cmd"),
                ]
            )

    binaries = ("node", "npm", "npx")
    if os.name != "nt":
        binaries += ("bash",)
    for binary in binaries:
        path = shutil.which(binary)
        if path:
            entries.append(os.path.dirname(path))

    return list(dict.fromkeys(entry for entry in entries if entry))


# No-Docker local profile: the Docker sandbox image bakes the Office CLI shims,
# Python dependencies, and Node modules into the image; the host runner needs
# explicit equivalents. Pass the site-building/Node env through and prepend
# only trusted executable directories to the clean PATH. No-op elsewhere.
if os.getenv("DEPLOY_PROFILE") == "local":
    for _k in (
        "SCRIPT_RUNNER_WORKSPACE",
        "SITE_TEMPLATE_HOME",
        "SITE_TEMPLATE_DIR",
        "SITE_NODE_BASE",
        "SITE_CACHE",
        "SITE_DIST",
    ):
        _v = os.getenv(_k)
        if _v:
            SAFE_ENV[_k] = _v
    _extra_path = _local_safe_path_entries()
    if _extra_path:
        SAFE_ENV["PATH"] = os.pathsep.join(
            _extra_path + ([SAFE_ENV["PATH"]] if SAFE_ENV["PATH"] else [])
        )
    # npm/vite need a writable HOME for cache/config; keep the real one locally.
    SAFE_ENV["HOME"] = os.getenv("HOME") or os.getenv("USERPROFILE") or _TEMP_ROOT

# Pre-create fontconfig cache dir once (avoids per-request mkdir)
Path(SAFE_ENV["XDG_CACHE_HOME"], "fontconfig").mkdir(parents=True, exist_ok=True)


class ExecuteRequest(BaseModel):
    script_content: str
    script_name: str
    language: str = "python"
    params: Dict[str, Any] = {}
    timeout: int = DEFAULT_TIMEOUT
    resource_files: Optional[Dict[str, str]] = None
    input_files: Optional[Dict[str, str]] = None
    input_files_b64: Optional[Dict[str, str]] = None


class FileOutput(BaseModel):
    name: str
    size: int
    content_b64: str
    mime_type: str


class ExecuteResponse(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    execution_time_ms: int
    files: List[FileOutput] = []


def _validate_filename(name: str) -> None:
    """Reject filenames with path traversal components."""
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(400, f"不安全的文件名: {name}")


def _validate_user_id(user_id: str) -> None:
    """Reject user_id values that could cause path traversal."""
    if not user_id or "/" in user_id or "\\" in user_id or ".." in user_id:
        raise HTTPException(400, f"不安全的 user_id: {user_id!r}")


class StageFile(BaseModel):
    name: str
    content_b64: str


class StageRequest(BaseModel):
    user_id: str
    files: List[StageFile]


class StageResponse(BaseModel):
    staged: List[Dict[str, str]]  # [{"name": ..., "path": ...}]


@app.post("/stage", response_model=StageResponse)
async def stage_files(req: StageRequest):
    """Stage files into /workspace/myspace/{user_id}/ so later code execution can read them directly by path."""
    _validate_user_id(req.user_id)
    base_dir = Path(f"{WORKSPACE_ROOT}/myspace/{req.user_id}")
    base_dir.mkdir(parents=True, exist_ok=True)

    staged = []
    for f in req.files:
        _validate_filename(f.name)
        try:
            content = base64.b64decode(f.content_b64)
        except Exception:
            raise HTTPException(400, f"文件 {f.name} 的 base64 内容无效")
        dest = base_dir / f.name
        dest.write_bytes(content)
        staged.append({"name": f.name, "path": str(dest)})

    return StageResponse(staged=staged)


@app.get("/health")
async def health():
    return {"status": "ok"}


class PutFileRequest(BaseModel):
    path: str
    content_b64: str


class GetFileRequest(BaseModel):
    path: str


class GetFileResponse(BaseModel):
    content_b64: str
    size: int


def _canon_ws(path: str) -> str:
    """Alias a container-canonical ``/workspace[/...]`` path to the real root.

    No-op in Docker (root == /workspace); in the no-Docker local profile the model
    and plugin backends pass /workspace paths that must map to SCRIPT_RUNNER_WORKSPACE.

    Mirror of ``core.llm.tools._paths.canonicalize_ws_path`` — this sidecar imports
    nothing from ``core`` (it ships as a standalone image), so the logic is copied;
    keep the two in sync.
    """
    if not isinstance(path, str) or WORKSPACE_ROOT == "/workspace":
        return path
    if path == "/workspace":
        return WORKSPACE_ROOT
    if path.startswith("/workspace/"):
        return WORKSPACE_ROOT.rstrip("/") + path[len("/workspace") :]
    return path


def _validate_workspace_path(path: str) -> Path:
    """Ensure the path is inside the workspace root and reject path traversal."""
    p = Path(_canon_ws(path)).resolve()
    workspace = Path(WORKSPACE_ROOT).resolve()
    try:
        p.relative_to(workspace)
    except ValueError:
        raise HTTPException(400, f"路径必须在 {WORKSPACE_ROOT} 下: {path}")
    return p


@app.post("/put_file")
async def put_file(req: PutFileRequest):
    """Write base64 bytes directly to the given sandbox path for later execute calls to reference.

    Difference from /execute's input_files_b64: files written via this endpoint are
    **not** cleaned up when execute finishes, which suits multi-step flows like
    sandbox_put_artifact ("stage first, then call bash").
    """
    p = _validate_workspace_path(req.path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = base64.b64decode(req.content_b64)
    except Exception:
        raise HTTPException(400, "base64 内容无效")
    p.write_bytes(content)
    return {"ok": True, "size": len(content)}


@app.post("/get_file", response_model=GetFileResponse)
async def get_file(req: GetFileRequest):
    """Read a file from the sandbox and return it base64-encoded. Used by sandbox_get_artifact to register outputs as artifacts."""
    p = _validate_workspace_path(req.path)
    if not p.is_file():
        raise HTTPException(404, f"文件不存在: {req.path}")
    data = p.read_bytes()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(413, f"文件过大: {len(data)} > {MAX_FILE_SIZE}")
    return GetFileResponse(
        content_b64=base64.b64encode(data).decode("ascii"),
        size=len(data),
    )


def _seed_text_files(
    work_dir: Path,
    file_dict: Optional[Dict[str, str]],
    seeded_files: set,
) -> None:
    """Write text files into work_dir and register them in seeded_files."""
    if not file_dict:
        return
    for fname, fcontent in file_dict.items():
        fpath = work_dir / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(fcontent, encoding="utf-8")
        seeded_files.add(str(fpath.relative_to(work_dir)))


def _seed_b64_files(
    work_dir: Path,
    file_dict: Optional[Dict[str, str]],
    seeded_files: set,
) -> None:
    """Write base64-decoded binary files into work_dir and register them in seeded_files."""
    if not file_dict:
        return
    for fname, b64content in file_dict.items():
        fpath = work_dir / fname
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_bytes(base64.b64decode(b64content))
        seeded_files.add(str(fpath.relative_to(work_dir)))


@app.post("/execute", response_model=ExecuteResponse)
async def execute(req: ExecuteRequest):
    # ── Basic validation ──
    if req.language not in INTERPRETERS:
        raise HTTPException(400, f"不支持的语言: {req.language}")
    if len(req.script_content) > MAX_SCRIPT_SIZE:
        raise HTTPException(400, f"脚本过大: {len(req.script_content)} > {MAX_SCRIPT_SIZE}")
    timeout = min(req.timeout, MAX_TIMEOUT)

    # Local profile: the model writes container-canonical /workspace/... paths (from
    # the system prompt, skills, and plugin scripts). Alias them to the real root so
    # bash/python that touch /workspace resolve.  A callable replacement is
    # essential on Windows because ``C:\\Users`` contains regex escape syntax.
    if WORKSPACE_ROOT != "/workspace":
        req.script_content = _rewrite_execution_paths(req.script_content, req.language)
        if isinstance(req.params, dict) and req.params:
            _args = req.params.get("_args")
            if isinstance(_args, list):
                req.params["_args"] = [
                    _rewrite_execution_paths(a, req.language) if isinstance(a, str) else a
                    for a in _args
                ]

    # ── Filename safety validation (prevent path traversal) ──
    _validate_filename(req.script_name)
    for file_dict in filter(None, [req.resource_files, req.input_files, req.input_files_b64]):
        for fname in file_dict:
            _validate_filename(fname)

    # ── Prepare temporary working directory ──
    Path(WORKSPACE_ROOT).mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="skill_", dir=WORKSPACE_ROOT))
    seeded_files: set[str] = set()
    # Snapshot existing files in the workspace root before execution
    _pre_existing_root_files: set = set()
    try:
        for _f in Path(WORKSPACE_ROOT).iterdir():
            if _f.is_file():
                _pre_existing_root_files.add(_f.name)
    except Exception:
        pass
    try:
        # Write the script file
        script_path = work_dir / req.script_name
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(req.script_content, encoding="utf-8")

        # Write resource files and input files (input_files after resource_files; same-name entries overwrite)
        _seed_text_files(work_dir, req.resource_files, seeded_files)
        _seed_text_files(work_dir, req.input_files, seeded_files)
        _seed_b64_files(work_dir, req.input_files_b64, seeded_files)

        # ── Execute ──
        interpreter = INTERPRETERS[req.language]
        t0 = time.monotonic()

        # Support CLI args: params._args list is appended to command line
        cli_args: list[str] = []
        stdin_params = dict(req.params)
        if "_args" in stdin_params:
            raw_args = stdin_params.pop("_args")
            if isinstance(raw_args, list):
                cli_args = [str(a) for a in raw_args]

        result = await _execute_subprocess(
            cmd=[*interpreter, str(script_path), *cli_args],
            stdin_data=json.dumps(stdin_params, ensure_ascii=False),
            timeout=timeout,
            cwd=str(work_dir),
        )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        result["execution_time_ms"] = elapsed_ms

        # ── Scan generated file outputs ──
        # LLM-generated code may write to work_dir (relative paths) or the workspace
        # root (absolute paths), so both locations must be scanned
        generated_files: List[dict] = []
        total_size = 0
        seen_names: set = set()
        # Track files already present in the workspace root before execution, to avoid collecting them by mistake
        workspace_root = Path(WORKSPACE_ROOT)

        def _collect_file(fpath: Path) -> bool:
            """Try to collect a file. Returns True if collected."""
            nonlocal total_size
            if not fpath.is_file():
                return False
            if fpath == script_path:
                return False
            if fpath.is_relative_to(work_dir):
                rel_path = str(fpath.relative_to(work_dir))
            else:
                rel_path = ""
            if rel_path and rel_path in seeded_files:
                return False
            if fpath.suffix.lower() not in ALLOWED_EXTENSIONS:
                return False
            if fpath.name in seen_names:
                return False
            fsize = fpath.stat().st_size
            if fsize == 0 or fsize > MAX_FILE_SIZE:
                return False
            if total_size + fsize > MAX_TOTAL_FILE_SIZE:
                return False
            if len(generated_files) >= MAX_FILE_COUNT:
                return False
            mime, _ = mimetypes.guess_type(str(fpath))
            with open(fpath, "rb") as fh:
                content_b64 = base64.b64encode(fh.read()).decode("ascii")
            generated_files.append(
                {
                    "name": fpath.name,
                    "size": fsize,
                    "content_b64": content_b64,
                    "mime_type": mime or "application/octet-stream",
                }
            )
            seen_names.add(fpath.name)
            total_size += fsize
            return True

        try:
            # 1) Scan work_dir (relative path outputs)
            for fpath in sorted(work_dir.rglob("*")):
                _collect_file(fpath)

            # 2) Scan /workspace/ root (absolute path outputs like /workspace/output.csv)
            #    Only collect NEW files (not pre-existing, not inside work_dir)
            for fpath in sorted(workspace_root.iterdir()):
                if fpath.is_dir():
                    continue
                if fpath.name in _pre_existing_root_files:
                    continue
                _collect_file(fpath)
        except Exception as e:
            logger.warning("file scan error: %s", e)

        result["files"] = generated_files

        return ExecuteResponse(**result)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        # Note: do NOT wipe /workspace root here. Files in /workspace are
        # intentionally durable between calls: sandbox_put_artifact stages
        # inputs and bash-emitted outputs are read back by sandbox_get_artifact.
        # The sidecar container's lifecycle is the cleanup boundary.


async def _execute_subprocess(cmd: list, stdin_data: str, timeout: int, cwd: str) -> Dict[str, Any]:
    """Execute a command in a restricted subprocess."""

    nproc_limit = _subprocess_nproc_limit(cmd)

    def _set_limits():
        # Keep the post-fork callback minimal: non-async-safe Python work in a
        # multi-threaded server's preexec_fn can deadlock before exec().
        if resource is not None and nproc_limit is not None:
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))

    if os.name == "nt":
        spawn_options: Dict[str, Any] = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        spawn_options = {
            # Host-local quick installs intentionally pass no preexec_fn at all.
            "preexec_fn": _set_limits if nproc_limit is not None else None,
            # Give every execution its own process group for descendant cleanup.
            "start_new_session": True,
        }

    proc: Optional[asyncio.subprocess.Process] = None
    # Do not expose PIPE file descriptors to document-tool descendants.  Some
    # renderers briefly fan out or leave a helper behind; an inherited pipe then
    # keeps ``communicate()`` waiting for EOF even after the requested CLI has
    # exited successfully.  Regular temporary files avoid that false timeout and
    # also prevent a verbose child from filling an OS pipe buffer.
    with (
        tempfile.TemporaryFile() as stdin_file,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        stdin_file.write(stdin_data.encode("utf-8"))
        stdin_file.seek(0)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=stdin_file,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=cwd,
                env=SAFE_ENV,
                **spawn_options,
            )
            await asyncio.wait_for(_wait_for_process_exit(proc), timeout=timeout)
            exit_code = proc.returncode or 0
            # A script can exit after starting a background helper.  Clean the
            # execution group on successful completion as well as on failure so
            # the quick-install service cannot accumulate orphan processes.
            await _terminate_process_group(proc)
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout_bytes = stdout_file.read(MAX_OUTPUT_BYTES)
            stderr_bytes = stderr_file.read(10240)
            return {
                "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                "exit_code": exit_code,
            }
        except asyncio.TimeoutError:
            await _terminate_process_group(proc)
            return {"stdout": "", "stderr": f"执行超时（{timeout}秒）", "exit_code": -1}
        except asyncio.CancelledError:
            # Client disconnects and server shutdown cancellation need the same
            # descendant cleanup as an ordinary execution timeout.
            await _terminate_process_group(proc)
            raise
        except Exception as e:
            await _terminate_process_group(proc)
            logger.exception("subprocess execution failed")
            detail = str(e)
            if (
                isinstance(e, FileNotFoundError)
                and os.name == "nt"
                and cmd
                and Path(str(cmd[0])).stem.lower() in {"bash", "hugagent-git-bash-not-installed"}
            ):
                detail = "Windows 本机未找到 Bash；请安装 Git for Windows 后重启桌面客户端"
            return {"stdout": "", "stderr": detail, "exit_code": -1}


def _subprocess_nproc_limit(cmd: list) -> Optional[int]:
    """Return the child limit that is safe for the selected deployment profile.

    Linux accounts ``RLIMIT_NPROC`` against the process' real UID, not against
    the child or its process tree.  The no-Docker quick-install profile shares
    its UID with the backend, MCP sidecars, desktop session, and every other
    process owned by the user.  Setting a limit of 64/128 there makes a child
    start successfully but prevents bash from forking as soon as the user's
    *total* process count reaches the limit.  Docker deployments have their own
    UID namespace plus a cgroup ``pids_limit``, so retain the defence in depth
    there and skip only the unsafe host-local limit.
    """
    if resource is None or os.name == "nt":
        return None
    if os.getenv("DEPLOY_PROFILE", "").strip().lower() == "local":
        return None

    # Do not limit RLIMIT_AS (virtual address space): mmap-ing .so shared libraries
    # needs lots of virtual address space; 256MB makes C extensions like lxml/numpy
    # fail with "failed to map segment from shared object".
    # Do not limit RLIMIT_FSIZE: internal file operations during .NET runtime startup trigger SIGXFSZ.
    # Actual disk usage is controlled at the container level by Docker tmpfs size and mem_limit.
    return 128 if cmd and cmd[0] in {"node", "bash"} else 64


async def _terminate_process_group(
    proc: Optional[asyncio.subprocess.Process],
) -> None:
    """Kill and reap one execution process together with all descendants."""
    if proc is None:
        return
    if os.name == "nt":
        # Once the leader has exited Windows may immediately recycle its PID;
        # taskkill on that stale PID could target an unrelated process. Timeout
        # and cancellation reach this branch while the leader is still alive.
        if proc.returncode is not None:
            return
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (AttributeError, PermissionError):
        # Defensive fallback for unusual POSIX runtimes where process-group
        # signalling is unavailable even though this service uses ``resource``.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    if proc.returncode is None:
        await _wait_for_process_exit(proc)


async def _wait_for_process_exit(proc: asyncio.subprocess.Process) -> int:
    """Wait until asyncio's child watcher has reaped the subprocess.

    ``Process.wait()`` has a race on some local quick-install runtimes when a
    very short-lived shell exits between waiter registration and the transport
    callback: ``returncode`` is already populated, yet the waiter is never
    resolved.  Polling the child-watcher-owned return code avoids that false
    timeout without doing our own ``waitpid`` or blocking the event loop.
    """
    while proc.returncode is None:
        await asyncio.sleep(0.02)
    return proc.returncode
