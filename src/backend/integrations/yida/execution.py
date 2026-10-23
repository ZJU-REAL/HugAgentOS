"""Yida command execution on the desktop host or in a persistent server sandbox."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import shlex
import sys
import hashlib
from core.infra.proc import no_window_kwargs
from pathlib import Path

from core.config.settings import settings

logger = logging.getLogger(__name__)
YIDA_WORKSPACE_MOUNT = "/home/ubuntu/yida-workspace"
_NO_WS_MARKER = "__JX_YIDA_NO_WS__"


def workspace_dir(user_id: str) -> Path:
    from integrations.yida.storage import yida_shared_workspace_dir
    from integrations.yida.storage import yida_workspace_dir

    if not settings.deploy.is_local and settings.sandbox.provider == "script_runner":
        return yida_shared_workspace_dir()
    return yida_workspace_dir(user_id)


def _utils_path() -> str | None:
    executable = shutil.which("openyida")
    if not executable:
        return None
    binary = Path(executable).resolve()
    candidates = [
        binary.parent.parent / "lib/node_modules/openyida",
        binary.parent / "node_modules/openyida",
        *[Path(root) / "openyida" for root in os.environ.get("NODE_PATH", "").split(os.pathsep) if root],
        *binary.parents,
    ]
    for root in candidates:
        utils = root / "lib/core/utils.js"
        if root.name == "openyida" and utils.is_file():
            return str(utils)
    return None


def bundled_cli() -> tuple[str, str, str] | None:
    executable = Path(sys.executable).absolute()
    for root in (executable.parent.parent, executable.parent.parent.parent):
        manifest_path = root / "plugin-clis.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema") != 1:
            raise RuntimeError("Bundled plugin CLI manifest is invalid")
        paths = [manifest["node"], manifest["openyida"]["script"], manifest["openyida"]["utils"]]
        resolved = []
        for relative in paths:
            path = (root / relative).resolve()
            path.relative_to(root.resolve())
            if not path.is_file():
                raise RuntimeError("Bundled OpenYida runtime is incomplete")
            resolved.append(str(path))
        if hashlib.sha256(Path(resolved[1]).read_bytes()).hexdigest() != manifest["openyida"]["sha256"]:
            raise RuntimeError("Bundled OpenYida integrity check failed")
        return tuple(resolved)
    return None


async def run_local(user_id: str, command: str, timeout: int) -> tuple[str, int]:
    arguments = shlex.split(command)
    env = os.environ.copy()
    try:
        private = bundled_cli()
    except (OSError, ValueError, KeyError, RuntimeError):
        return json.dumps({"error": "随包宜搭运行时损坏，请修复或升级客户端。"}), 127
    if private:
        node, script, utils = private
        if arguments[0] == "openyida":
            arguments = [node, script, *arguments[1:]]
        elif arguments[0] == "node":
            arguments[0] = node
        else:
            raise ValueError("unsupported local Yida command")
        env["HUGAGENT_OPENYIDA_UTILS"] = utils
    else:
        from core.sandbox.runner.runtime_tools import resolve_bash_executable
        bash = resolve_bash_executable()
        if not bash or not shutil.which("openyida"):
            return json.dumps({"error": "本机缺少 OpenYida CLI，请升级完整客户端或准备运行依赖。"}), 127
        arguments = [bash, "-c", "exec " + command]
        utils = _utils_path()
        if utils:
            env["HUGAGENT_OPENYIDA_UTILS"] = utils
    workspace = workspace_dir(user_id)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = await asyncio.create_subprocess_exec(
        *arguments, cwd=str(workspace), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **no_window_kwargs(),
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
        code = process.returncode or 0
        if code:
            # Classify without printing CLI output: it may contain credentials.
            diagnostic = err.decode("utf-8", errors="replace").casefold()
            reason = ("dependency_missing" if "cannot find module" in diagnostic
                      else "permission_denied" if "permission denied" in diagnostic or "eacces" in diagnostic
                      else "cli_failed")
            logger.warning("yida_local_command_failed user=%s exit_code=%s reason=%s", user_id, code, reason)
        return out.decode("utf-8", errors="replace"), code
    except (asyncio.TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        return json.dumps({"error": "宜搭登录请求超时，请重试。"}), 124


async def run_sandbox(user_id: str, sub_command: str, timeout: int) -> tuple[str, int]:
    from core.sandbox.factory import get_sandbox_provider
    from core.sandbox.protocol import ProcessRequest

    provider = get_sandbox_provider()
    session_id = f"yida-connect-{user_id}"
    guarded = (
        f"cd {YIDA_WORKSPACE_MOUNT} 2>/dev/null "
        f"|| {{ echo {_NO_WS_MARKER}; exit 97; }}; {sub_command}"
    )
    req = ProcessRequest(
        script_content=guarded,
        script_name="_yida_connect.sh",
        language="bash",
        timeout=timeout,
        user_id=user_id,
        session_id=session_id,
    )
    for attempt in range(3):
        result = await provider.run_to_completion(req)
        out = result.stdout or ""
        err = getattr(result, "stderr", "") or ""
        rc = int(result.exit_code or 0)
        stale = _NO_WS_MARKER in out or rc == 127 or "command not found" in err
        if stale and attempt < 2:
            logger.info(
                "[yida] 检测到陈旧沙箱（无工作目录挂载或无 openyida），销毁重建 "
                "user=%s attempt=%d",
                user_id,
                attempt + 1,
            )
            try:
                await provider.close_session(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[yida] close_session 失败 user=%s: %s", user_id, exc)
            continue
        if rc != 0:
            logger.info(
                "[yida] 沙箱命令非零退出 user=%s rc=%s stderr=%.300s",
                user_id,
                rc,
                err,
            )
        return out, rc
    return "", 97  # theoretically unreachable (the loop always returns); a fuse
