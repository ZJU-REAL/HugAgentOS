"""SDK binding for the user-scoped batch job action service."""
from core.llm.factory.tools.definitions import function_tool
import json
from typing import Any, Dict, List, Optional
from agentscope.message import TextBlock
from agentscope.tool import Toolkit
from agentscope.tool._response import ToolChunk
from core.harness.registry.job_operations import job_operations

def register_run_job(
    toolkit: Toolkit,
    *,
    user_id: str,
    chat_id: Optional[str],
    sandbox_session_id: Optional[str],
    allowed_tools: Optional[List[str]] = None,
    model_name: Optional[str] = None,
    model_provider_id: Optional[str] = None,
    project_ctx: Optional[Dict[str, Any]] = None,
    parent_runtime: Optional[Dict[str, Any]] = None,
) -> None:
    """注册 run_job。

    ``parent_runtime`` 保存主智能体的能力配置；每个 agent() 使用独立上下文派生实例。
    ``allowed_tools`` 保留脚本显式选择 MCP 的权限检查。
    """

    execute = job_operations(user_id=user_id, chat_id=chat_id, sandbox_session_id=sandbox_session_id, allowed_tools=allowed_tools, model_name=model_name, model_provider_id=model_provider_id, project_ctx=project_ctx, parent_runtime=parent_runtime)

    async def run_job(
        action: str = "start",
        script_path: str = "",
        name: str = "",
        job_id: str = "",
        wait: bool = False,
        # 默认必须是**带三方库**的那个解释器。沙箱里裸 python3 是干净的系统解释器，
        # 连 openpyxl/pandas 都没有——默认值写成 python3 的直接后果是作业脚本第一行
        # import 就 ModuleNotFoundError 当场失败（实测踩过）。$PY_BIN 指向预装全套数据
        # 依赖的解释器，取不到时才退回 python3。
        interpreter: str = "${PY_BIN:-python3}",
        max_calls: int = 0,
        max_seconds: int = 0,
        concurrency: int = 8,
        dest_path: str = "",
        status: str = "",
        progress_wake_sec: int = 300,
        on_conflict: str = "block",
    ) -> ToolChunk:
        payload = await execute(action=action, script_path=script_path, name=name, job_id=job_id, wait=wait, interpreter=interpreter, max_calls=max_calls, max_seconds=max_seconds, concurrency=concurrency, dest_path=dest_path, status=status, progress_wake_sec=progress_wake_sec, on_conflict=on_conflict)
        return ToolChunk(content=[TextBlock(type="text", text=json.dumps(payload, ensure_ascii=False))])

    from .job_description import RUN_JOB_DESCRIPTION
    run_job.__doc__ = RUN_JOB_DESCRIPTION
    toolkit.append(function_tool(run_job))
