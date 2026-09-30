"""Bounded sandbox file operations for job scripts and exports."""

import base64
from typing import Tuple

from core.sandbox import ProcessRequest, get_sandbox_provider


def _job_directory(session_id: str, job_row_id: str) -> str:
    from core.llm.tools._paths import workspace_directory

    return f"{workspace_directory(session_id)}/.job/{job_row_id}"


def _quote_path(path: str) -> str:
    from core.llm.tools._paths import path_rules

    return path_rules().quote_shell_path(path)


async def _sbx_bash(command: str, *, session_id: str, user_id: str, timeout: int = 60):
    """在持久沙箱里跑一段 bash，返回 (exit_code, stdout, stderr)。"""
    from core.sandbox import ProcessRequest, get_sandbox_provider

    provider = get_sandbox_provider()
    res = await provider.run_to_completion(
        ProcessRequest(
            script_content=command,
            script_name="job_ctl.sh",
            language="bash",
            timeout=timeout,
            session_id=session_id,
            user_id=user_id,
        )
    )
    return res.exit_code, (res.stdout or ""), (res.stderr or "")


async def write_sandbox_file(
    path: str, content: str, *, session_id: str, user_id: str
) -> Tuple[bool, str]:
    """把文本写进沙箱，**分块 + 读回校验**。返回 (是否成功, 说明)。

    为什么不能一条 `echo '<b64>' | base64 -d > f` 了事：沙箱 execute 对命令体积有上限，
    超了之后**静默失败**——exit=0、stderr 为空、文件却不存在（实测拐点在 b64 约
    170KB；568 行台账导出正好落在这个区间，于是"导出成功"但文件从来没出现过）。
    所以这里按块追加，并且以**沙箱里读回的真实字节数**为准，绝不用调用方的计数报成功。
    """
    path = _quote_path(path)
    raw = (content or "").encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    # 单块 48KB b64（≈36KB 原文），远离静默失败拐点
    chunk = 48_000
    parts = [b64[i : i + chunk] for i in range(0, len(b64), chunk)] or [""]

    code, out, err = await _sbx_bash(
        f'mkdir -p "$(dirname -- {path})" && : > {path}.b64',
        session_id=session_id,
        user_id=user_id,
        timeout=60,
    )
    if code != 0:
        return False, f"创建目标失败: {(err or out)[:200]}"

    for idx, part in enumerate(parts):
        code, out, err = await _sbx_bash(
            f"printf '%s' '{part}' >> {path}.b64",
            session_id=session_id,
            user_id=user_id,
            timeout=90,
        )
        if code != 0:
            return False, f"第 {idx + 1}/{len(parts)} 块写入失败: {(err or out)[:200]}"

    code, out, err = await _sbx_bash(
        f"base64 -d {path}.b64 > {path} && rm -f {path}.b64 && wc -c < {path}",
        session_id=session_id,
        user_id=user_id,
        timeout=90,
    )
    if code != 0:
        return False, f"解码失败: {(err or out)[:200]}"

    written = "".join((out or "").split())
    if not written.isdigit() or int(written) != len(raw):
        return False, f"落盘校验不通过：期望 {len(raw)} 字节，沙箱读回 {written or '(空)'}"
    return True, f"{len(raw)} 字节 / {len(parts)} 块"


async def read_runner_log(job_row_id: str, *, user_id: str, session_id: str, tail: int = 40) -> str:
    from .state import snapshot

    row = snapshot(job_row_id)
    if row["user_id"] != user_id or row["session_id"] != session_id:
        raise PermissionError("Job owner mismatch")
    directory = row["meta"].get("execution", {}).get("directory")
    if not directory:
        return ""
    code, out, _ = await _sbx_bash(
        f"tail -n {int(tail)} {_quote_path(directory + '/runner.log')} 2>/dev/null | base64 -w0 || true",
        session_id=session_id,
        user_id=user_id,
        timeout=30,
    )
    if code != 0 or not (out or "").strip():
        return ""
    try:
        return base64.b64decode("".join(out.split())).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return out
