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
import resource
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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
MAX_SCRIPT_SIZE = 512 * 1024    # 512KB

# Local-profile /workspace→real-root rewrite for executed script text. Compiled
# once here (invariant: WORKSPACE_ROOT is read from env at import). None in Docker,
# where the roots are equal and no rewrite is needed. Match /workspace only at a
# path boundary so an unrelated substring like /workspaces is left alone.
_WS_REWRITE = (
    (re.compile(r'/workspace(?=/|$|["\'\s:;)&|])'), WORKSPACE_ROOT.rstrip("/"))
    if WORKSPACE_ROOT != "/workspace" else None
)

INTERPRETERS = {
    "python": ["python3", "-u"],
    "bash": ["bash"],
    "javascript": ["node"],
}

# ── Generated-file capture ──
MAX_FILE_SIZE = 10 * 1024 * 1024      # 10MB per file
MAX_TOTAL_FILE_SIZE = 20 * 1024 * 1024  # 20MB total
MAX_FILE_COUNT = 20
ALLOWED_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".csv", ".xlsx", ".xls", ".json", ".txt", ".pdf",
    ".html", ".htm", ".docx", ".pptx", ".md",
}

# Clean environment variables — leak no sensitive information
SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "XDG_CACHE_HOME": "/tmp/.cache",
    "FONTCONFIG_PATH": "/etc/fonts",
    "FONTCONFIG_FILE": "/etc/fonts/fonts.conf",
    "LANG": "en_US.UTF-8",
    "PYTHONIOENCODING": "utf-8",
    "MPLBACKEND": "Agg",  # matplotlib non-interactive backend
    "OPENBLAS_NUM_THREADS": "1",  # prevent OpenBLAS from allocating lots of thread memory
    "OMP_NUM_THREADS": "1",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",  # disable dotnet telemetry
    "DOTNET_NOLOGO": "1",  # suppress dotnet startup banner
    "DOTNET_EnableDiagnostics": "0",  # stop dotnet from creating diagnostic pipes/core dump files
}
for _key in ("NODE_PATH", "PLAYWRIGHT_BROWSERS_PATH"):
    _val = os.getenv(_key)
    if _val:
        SAFE_ENV[_key] = _val

# No-Docker local profile: the Docker sandbox image bakes node/npm into /usr/bin and
# the site-building template env into the image; the host subprocess runner has
# neither. Pass the site-building env through and add the host's node/npm dirs to
# PATH so React path-B building (init script → npm build) resolves. No-op elsewhere.
if os.getenv("DEPLOY_PROFILE") == "local":
    import shutil as _shutil

    for _k in ("SCRIPT_RUNNER_WORKSPACE", "SITE_TEMPLATE_HOME", "SITE_TEMPLATE_DIR",
               "SITE_NODE_BASE", "SITE_CACHE", "SITE_DIST"):
        _v = os.getenv(_k)
        if _v:
            SAFE_ENV[_k] = _v
    _extra_path: list = []
    for _bin in ("node", "npm", "npx"):
        _p = _shutil.which(_bin)
        if _p:
            _d = os.path.dirname(_p)
            if _d and _d not in _extra_path:
                _extra_path.append(_d)
    if _extra_path:
        SAFE_ENV["PATH"] = os.pathsep.join(_extra_path + [SAFE_ENV["PATH"]])
    # npm/vite need a writable HOME for cache/config; keep the real one locally.
    SAFE_ENV["HOME"] = os.getenv("HOME", "/tmp")

# Pre-create fontconfig cache dir once (avoids per-request mkdir)
Path("/tmp/.cache/fontconfig").mkdir(parents=True, exist_ok=True)


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
        return WORKSPACE_ROOT.rstrip("/") + path[len("/workspace"):]
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
    # bash/python that touch /workspace resolve. _WS_REWRITE is None in Docker.
    if _WS_REWRITE is not None:
        _re, _repl = _WS_REWRITE
        _canon = lambda s: _re.sub(_repl, s) if isinstance(s, str) else s
        req.script_content = _canon(req.script_content)
        if isinstance(req.params, dict) and req.params:
            _args = req.params.get("_args")
            if isinstance(_args, list):
                req.params["_args"] = [_canon(a) if isinstance(a, str) else a for a in _args]

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
            generated_files.append({
                "name": fpath.name,
                "size": fsize,
                "content_b64": content_b64,
                "mime_type": mime or "application/octet-stream",
            })
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


async def _execute_subprocess(
    cmd: list, stdin_data: str, timeout: int, cwd: str
) -> Dict[str, Any]:
    """Execute a command in a restricted subprocess."""

    def _set_limits():
        # Do not limit RLIMIT_AS (virtual address space): mmap-ing .so shared libraries
        # needs lots of virtual address space; 256MB makes C extensions like lxml/numpy
        # fail with "failed to map segment from shared object".
        # Do not limit RLIMIT_FSIZE: internal file operations during .NET runtime startup trigger SIGXFSZ.
        # Actual disk usage is controlled at the container level by Docker tmpfs size and mem_limit.
        nproc_limit = 128 if cmd and cmd[0] in {"node", "bash"} else 64
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=SAFE_ENV,
            preexec_fn=_set_limits,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin_data.encode("utf-8")),
            timeout=timeout,
        )
        return {
            "stdout": stdout_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES],
            "stderr": stderr_bytes.decode("utf-8", errors="replace")[:10240],
            "exit_code": proc.returncode or 0,
        }
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"stdout": "", "stderr": f"执行超时（{timeout}秒）", "exit_code": -1}
    except Exception as e:
        logger.exception("subprocess execution failed")
        return {"stdout": "", "stderr": str(e), "exit_code": -1}
