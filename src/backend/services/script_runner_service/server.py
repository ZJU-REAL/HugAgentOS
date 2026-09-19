"""
Skill script execution sidecar service.

Receives HTTP requests from the backend and executes predefined scripts in a
restricted subprocess. This service runs in a separate container with no
database/Redis/API-key access.
"""

import asyncio
import base64
import hmac
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    import resource
except ImportError:  # Windows does not provide the POSIX resource module.
    resource = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("script-runner")

if __package__:
    from .workspace_paths import session_root
    from .runtime_tools import resolve_bash_executable as _resolve_bash_executable
    from .runtime_tools import windows_tool_path_entries
else:
    from workspace_paths import session_root
    from runtime_tools import resolve_bash_executable as _resolve_bash_executable
    from runtime_tools import windows_tool_path_entries

# 与后端共享的密钥。这个服务以当前用户身份执行任意命令，本机形态下又监听在回环口上
# ——同机任何进程（包括刚被 OS 沙箱关起来的那条命令）都够得着。没有它，调用方只要不带
# sandbox_launch 再调一次 /execute，就能拿到一个完全不受约束的子进程。
_AUTH_TOKEN = (os.getenv("SANDBOX_RUNNER_TOKEN") or "").strip()


def _require_token(request: Request) -> None:
    if not _AUTH_TOKEN:
        return
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(value.strip(), _AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="unauthorized")


app = FastAPI(
    title="HugAgentOS Script Runner",
    docs_url=None,
    redoc_url=None,
    # 没有对外契约要发布；发布了反而等于给同机的探测者一份端点清单。
    openapi_url=None,
    dependencies=[Depends(_require_token)],
)

# ── Configuration ──
MAX_TIMEOUT = int(os.getenv("SCRIPT_MAX_TIMEOUT", "120"))
DEFAULT_TIMEOUT = int(os.getenv("SCRIPT_DEFAULT_TIMEOUT", "30"))
MAX_MEMORY_MB = int(os.getenv("SCRIPT_MAX_MEMORY_MB", "256"))
# Workspace root. In the Docker sidecar this stays the container-absolute
# ``/workspace`` (a mounted tmpfs). In the no-Docker local profile the runner is
# a plain host subprocess, so the CLI points it at a real host dir such as
# ``~/.hugagent/workspace`` via ``SCRIPT_RUNNER_WORKSPACE``. Everything under here
# is created on first use; ``.sessions/<hash>`` is the per-conversation boundary
# and ``_validate_workspace_path`` confines file API access to that directory.
WORKSPACE_ROOT = os.getenv("SCRIPT_RUNNER_WORKSPACE", "/workspace")
# Skills reach this container as two read-only mounts (see docker-compose.yml):
# the shared tree, and the root holding every user's per-user view. A session
# gets its own user's view linked in as ``<workspace>/skills``.
SESSION_WORKSPACES_DIR = ".sessions"
MAX_OUTPUT_BYTES = 1024 * 1024  # 1MB
MAX_SCRIPT_SIZE = 512 * 1024  # 512KB
MAX_ARTIFACT_EXPORT_BYTES = max(
    1, int(os.getenv("SANDBOX_ARTIFACT_MAX_BYTES", str(100 * 1024 * 1024)))
)

def _workspace_rules():
    if __package__:
        from . import desktop_workspace, sandbox_workspace
    else:
        import desktop_workspace
        import sandbox_workspace
    return desktop_workspace if os.getenv("DEPLOY_PROFILE", "").strip().lower() == "local" else sandbox_workspace


def _validate_session_id(session_id: str) -> str:
    """Validate a logical conversation id before deriving its opaque path key."""
    value = (session_id or "").strip()
    if not value:
        raise HTTPException(400, "session_id 不能为空")
    if len(value) > 512:
        raise HTTPException(400, "session_id 过长")
    return value


def _session_workspace(
    session_id: str,
    *,
    create: bool = False,
    user_id: Optional[str] = None,
    capability_view_key: Optional[str] = None,
) -> Path:
    """Return the durable filesystem root owned by one conversation session."""
    value = _validate_session_id(session_id)
    workspace = Path(session_root(WORKSPACE_ROOT, value))
    if create:
        workspace.mkdir(parents=True, exist_ok=True)
        if user_id:
            _validate_user_id(user_id)
        _workspace_rules().prepare_workspace(
            workspace, WORKSPACE_ROOT, user_id, capability_view_key,
        )
    return workspace


_BASH_EXECUTABLE = _resolve_bash_executable()

INTERPRETERS = {
    # Use the running venv on local Windows/macOS/Linux installations.  A bare
    # ``python3`` is not installed on a standard Windows machine.
    "python": [sys.executable, "-u"],
    "bash": [_BASH_EXECUTABLE or "hugagent-git-bash-not-installed"],
    "javascript": [shutil.which("node") or "node"],
}

# ── Generated-file capture ──
MAX_FILE_SIZE = MAX_ARTIFACT_EXPORT_BYTES
MAX_TOTAL_FILE_SIZE = MAX_ARTIFACT_EXPORT_BYTES
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
    "PYTHONDONTWRITEBYTECODE": "1",  # package revisions stay immutable during Python imports
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
for _key in ("NODE_PATH", "PLAYWRIGHT_BROWSERS_PATH", "JX_FONT_DIR", "PDF_SKILL_DIR"):
    _val = os.getenv(_key)
    if _val:
        SAFE_ENV[_key] = _val

_LOCAL_SKILL_CLI_IDS = ("pdf-editing",)


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
        entries.extend(windows_tool_path_entries(_BASH_EXECUTABLE))

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
    # Keep the bundled native tool at the version verified by the desktop release.
    SAFE_ENV["OFFICECLI_SKIP_UPDATE"] = "1"
    SAFE_ENV["PY_BIN"] = sys.executable
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


class SandboxLaunch(BaseModel):
    """Wire form of ``core.sandbox.oslayer.SandboxLaunch``.

    Declared here rather than imported so the sidecar stays a standalone service
    with no backend package on its path — the Docker image ships only this
    directory. The two definitions are joined by the JSON on the wire, and the
    backend's own test suite asserts they agree.

    ``spawn_plan`` is the form Windows uses: its confinement lives in an access
    token that has to be attached while the process is created, so there is no
    command that could wrap another command into it.
    """

    backend: str = ""
    argv_prefix: List[str] = []
    env: Dict[str, str] = {}
    spawn_plan: Optional[Dict[str, Any]] = None


class ExecuteRequest(BaseModel):
    script_content: str
    script_name: str
    language: str = "python"
    params: Dict[str, Any] = {}
    timeout: int = DEFAULT_TIMEOUT
    resource_files: Optional[Dict[str, str]] = None
    input_files: Optional[Dict[str, str]] = None
    input_files_b64: Optional[Dict[str, str]] = None
    session_id: str
    user_id: Optional[str] = None
    capability_view_key: Optional[str] = None
    # OS-level confinement for this execution, decided by the backend's
    # permission layer: an argv prefix to put in front of the interpreter and an
    # environment overlay. The sidecar applies both verbatim and never decides
    # whether an execution should be confined — that call belongs to whoever
    # knows the user's permission preset.
    sandbox_launch: Optional[SandboxLaunch] = None


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
        staged.append({"name": f.name, "path": f"/workspace/myspace/{req.user_id}/{f.name}"})

    return StageResponse(staged=staged)


@app.get("/health")
async def health():
    return {"status": "ok"}


class PutFileRequest(BaseModel):
    session_id: str
    user_id: Optional[str] = None
    path: str
    content_b64: str


class GetFileRequest(BaseModel):
    session_id: str
    user_id: Optional[str] = None
    path: str


class GetFileResponse(BaseModel):
    content_b64: str
    size: int


def _validate_workspace_path(
    path: str,
    session_id: str,
    user_id: Optional[str] = None,
    *,
    read_only: bool = False,
) -> Path:
    """Confine file APIs to this session plus its explicitly bound MySpace.

    相对路径按这个会话的工作目录解析——那本来就是执行时的 cwd。绝对路径原样用，
    不做任何改写。
    """
    workspace = _session_workspace(session_id, create=True, user_id=user_id).resolve()
    rules = _workspace_rules()
    p = rules.file_path(path, workspace, WORKSPACE_ROOT).resolve()
    allowed_roots = [workspace, *(p.resolve() for p in rules.extra_roots(WORKSPACE_ROOT, user_id, read_only=read_only))]
    for allowed in allowed_roots:
        try:
            p.relative_to(allowed)
            break
        except ValueError:
            continue
    else:
        raise HTTPException(400, f"路径必须在当前会话工作目录 {workspace} 下: {path}")
    return p


@app.post("/put_file")
async def put_file(req: PutFileRequest):
    """Write base64 bytes directly to the given sandbox path for later execute calls to reference.

    Difference from /execute's input_files_b64: files written via this endpoint are
    **not** cleaned up when execute finishes, which suits multi-step flows like
    sandbox_put_artifact ("stage first, then call bash").
    """
    p = _validate_workspace_path(req.path, req.session_id, req.user_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = base64.b64decode(req.content_b64)
    except Exception:
        raise HTTPException(400, "base64 内容无效")
    p.write_bytes(content)
    return {"ok": True, "size": len(content)}


# /get_file serves legacy Base64 consumers and the internal site-publish flow,
# whose tar pack is allowed up to 40MB (internal_sites
# MAX_PACK_BYTES) — a fetch cap below that makes larger site publishes fail
# after a successful in-sandbox tar. Keep a generous ceiling here; callers
# enforce their own tighter budgets.
MAX_FETCH_FILE_SIZE = 64 * 1024 * 1024


@app.post("/get_file", response_model=GetFileResponse)
async def get_file(req: GetFileRequest):
    """Read a file from the sandbox and return it base64-encoded."""
    p = _validate_workspace_path(req.path, req.session_id, req.user_id, read_only=True)
    if not p.is_file():
        raise HTTPException(404, f"文件不存在: {req.path}")
    data = p.read_bytes()
    if len(data) > MAX_FETCH_FILE_SIZE:
        raise HTTPException(413, f"文件过大: {len(data)} > {MAX_FETCH_FILE_SIZE}")
    return GetFileResponse(
        content_b64=base64.b64encode(data).decode("ascii"),
        size=len(data),
    )


@app.post("/get_file_raw", response_class=FileResponse)
async def get_file_raw(req: GetFileRequest) -> FileResponse:
    """Stream a sandbox file without Base64 expansion."""
    p = _validate_workspace_path(req.path, req.session_id, req.user_id, read_only=True)
    if not p.is_file():
        raise HTTPException(404, f"文件不存在: {req.path}")
    size = p.stat().st_size
    if size > MAX_ARTIFACT_EXPORT_BYTES:
        raise HTTPException(
            413,
            f"文件过大: {size} > {MAX_ARTIFACT_EXPORT_BYTES}",
        )
    media_type = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return FileResponse(
        path=p,
        media_type=media_type,
        filename=p.name,
        headers={"X-Artifact-Size": str(size)},
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

    # Each conversation owns a durable cwd; the selected profile owns path semantics.
    session_workspace = _session_workspace(
        req.session_id,
        create=True,
        user_id=req.user_id,
        capability_view_key=req.capability_view_key,
    )
    req.script_content = _workspace_rules().execution_text(
        req.script_content, req.language, session_workspace, req.user_id,
    )
    if isinstance(req.params, dict):
        _args = req.params.get("_args")
        if isinstance(_args, list):
            req.params["_args"] = [
                _workspace_rules().execution_text(a, req.language, session_workspace, req.user_id)
                if isinstance(a, str) else a for a in _args
            ]
    # ── Filename safety validation (prevent path traversal) ──
    _validate_filename(req.script_name)
    for file_dict in filter(None, [req.resource_files, req.input_files, req.input_files_b64]):
        for fname in file_dict:
            _validate_filename(fname)

    # 工作目录就是这个对话自己的目录，不另开临时子目录。
    work_dir = session_workspace
    seeded_files: set[str] = set()
    _pre_existing_root_files: set = set()
    try:
        for _f in session_workspace.iterdir():
            if _f.is_file():
                _pre_existing_root_files.add(_f.name)
    except Exception:
        pass
    try:
        # The cwd is durable, but each invocation owns a distinct script.
        # O_EXCL creation prevents overwriting a user's file or another tool call.
        fd, script_file = tempfile.mkstemp(
            prefix=".__exec_", suffix=Path(req.script_name).suffix, dir=work_dir,
        )
        script_path = Path(script_file)
        with os.fdopen(fd, "w", encoding="utf-8") as script:
            script.write(req.script_content)

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
            sandbox_launch=req.sandbox_launch,
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
        workspace_root = session_workspace

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
            # work_dir 就是会话工作目录：只收本次新产生的根文件，已存在的不重复回传。
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
        # 目录本身不删；只清掉本次写进去的脚本文件。
        try:
            script_path.unlink(missing_ok=True)
        except (OSError, NameError):
            pass


class SessionRequest(BaseModel):
    session_id: str


@app.post("/sessions/close")
async def close_session(req: SessionRequest):
    """Delete exactly one conversation workspace."""
    workspace = _session_workspace(req.session_id)
    existed = workspace.exists()
    if existed:
        shutil.rmtree(workspace, ignore_errors=True)
    return {"closed": existed}


@app.post("/sessions/touch")
async def touch_session(req: SessionRequest):
    """Refresh one existing conversation workspace's activity timestamp."""
    workspace = _session_workspace(req.session_id)
    if not workspace.is_dir():
        return {"touched": False}
    os.utime(workspace, None)
    return {"touched": True}


async def _execute_subprocess(
    cmd: list,
    stdin_data: str,
    timeout: int,
    cwd: str,
    sandbox_launch: Optional[SandboxLaunch] = None,
) -> Dict[str, Any]:
    """Execute a command in a restricted subprocess.

    ``sandbox_launch`` applies the host OS's confinement. Most platforms express
    it as a wrapper command, so it simply goes in front of the argv; Windows
    expresses it as an access token, which has to be attached while the process
    is created and therefore takes the ``spawn_plan`` path below. The resource
    limits are computed from the *original* command either way, because they
    describe what the interpreter needs — the sandbox in front of it is not the
    workload.

    The limits and the sandbox never collide, and it is worth knowing why: a
    launch only ever arrives under the local profile, which is exactly the
    profile where the uid-wide ``RLIMIT_NPROC`` cap is deliberately not applied.
    Were it applied, bubblewrap would fail to create its user namespace on any
    machine whose login user already has more processes than the cap.
    """

    nproc_limit = _subprocess_nproc_limit(cmd)
    env = dict(SAFE_ENV)
    env.update(_workspace_rules().subprocess_environment(cwd))
    spawn_plan = None
    if sandbox_launch is not None:
        cmd = [*sandbox_launch.argv_prefix, *cmd]
        env.update(sandbox_launch.env)
        spawn_plan = sandbox_launch.spawn_plan

    def _set_limits():
        # Keep the post-fork callback minimal: non-async-safe Python work in a
        # multi-threaded server's preexec_fn can deadlock before exec().
        if resource is not None and nproc_limit is not None:
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))

    if os.name == "nt":
        # 桌面本机模式：服务自身没有控制台，被执行的命令若不显式禁用，会为每次
        # 执行新开一个黑色 cmd 窗口。
        spawn_options: Dict[str, Any] = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW,
        }
    else:
        spawn_options = {
            # Host-local quick installs intentionally pass no preexec_fn at all.
            "preexec_fn": _set_limits if nproc_limit is not None else None,
            # Give every execution its own process group for descendant cleanup.
            "start_new_session": True,
        }

    # Either an asyncio child or, under a Windows sandbox plan, a token-backed
    # process exposing the same pid/returncode/kill/wait surface.
    proc: Optional[Any] = None
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
            if spawn_plan is not None:
                proc = _spawn_with_sandbox_plan(
                    spawn_plan, cmd, cwd=cwd, env=env, files=(stdin_file, stdout_file, stderr_file)
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=stdin_file,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=cwd,
                    env=env,
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
        finally:
            close = getattr(proc, "close", None)
            if close is not None:
                close()


def _spawn_with_sandbox_plan(plan: Dict[str, Any], cmd: list, *, cwd, env, files):
    """Start a command under the Windows sandbox plan the backend decided on.

    A plan we cannot apply is an error, never a reason to start the command
    anyway: the layers above have already told the user this command is
    confined. The import is local because it is only reachable on Windows local
    mode, where the full backend tree is on the path — the Docker image ships
    this directory alone and never produces a plan to begin with.
    """
    from core.sandbox.oslayer.windows_runtime import spawn_confined

    stdin_file, stdout_file, stderr_file = files
    return spawn_confined(
        plan,
        cmd,
        cwd=cwd,
        env=env,
        stdin=stdin_file.fileno(),
        stdout=stdout_file.fileno(),
        stderr=stderr_file.fileno(),
    )


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
    proc: Optional[Any],
) -> None:
    """Kill and reap one execution process together with all descendants.

    Works against either an asyncio child or the token-backed process a Windows
    sandbox plan produces: both expose ``pid`` / ``returncode`` / ``kill`` /
    ``wait``, which is the whole surface used here.
    """
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


async def _wait_for_process_exit(proc: Any) -> int:
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
