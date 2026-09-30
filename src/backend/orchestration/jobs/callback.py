"""Probe callback reachability per sandbox, without a process-wide cache."""

import base64
import json
import logging
import os
from typing import List

from .files import _sbx_bash

logger = logging.getLogger(__name__)


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def callback_base_candidates() -> List[str]:
    """沙箱回调后端的候选基址，按可靠性排序。

    ⚠️ 这里**不能**只有一个写死的默认值。沙箱与后端的网络关系随部署形态而变：
    本机开发时沙箱不在 compose 网络里（只有宿主映射端口可达），而 HugAgentOS /
    主测试机上沙箱容器与 backend 同在一张 docker 网络（服务名可达，
    ``host.docker.internal`` 反而**解析不了**）。写死宿主的后果实测过：runner 起来后
    第一发回调就 ``Name or service not known`` 当场死掉，作业永远停在 pending、
    台账一条没有——用户只看见状态条上一个转圈的菊花，什么都不知道。

    所以改成候选表 + 启动前从沙箱里真探一次（见 ``resolve_callback_base``）。
    ``JOB_CALLBACK_URL`` 仍然是最高优先级的手动覆盖。
    """
    env = (os.environ.get("JOB_CALLBACK_URL") or "").strip()
    if env:
        return [env.rstrip("/")]
    port = (os.environ.get("PORT") or os.environ.get("BACKEND_PORT") or "3001").strip()
    return [
        f"http://backend:{port}",  # 同网 docker：服务名直连后端（后端自身不带 /api 前缀）
        "http://frontend/api",  # 同网 docker：经前端 nginx 反代（/api 由它剥掉）
        "http://host.docker.internal:3000/api",  # 沙箱不在同网：回宿主映射端口
    ]


# 探测脚本：在沙箱里逐个候选打 /health，第一个应答的即选中。只用标准库，
# 因为沙箱镜像不保证有 curl。
_PROBE_SOURCE = r"""import json, sys, urllib.request

for base in json.loads(sys.argv[1]):
    try:
        with urllib.request.urlopen(base + "/health", timeout=4) as resp:
            if resp.status < 500:
                print("PICK " + base)
                sys.exit(0)
    except Exception:
        continue
print("NONE")
"""


async def resolve_callback_base(*, session_id: str, user_id: str) -> str:
    """从沙箱里探出一个真正可达的回调基址；一个都不通就抛错（**不许静默启动**）。

    宁可在提交作业这一步就失败——错误会原样回到模型和用户手上，而"启动成功但永远
    没有进度"是最贵的失败形态：驱动干等、状态条转圈、用户等一小时才发现什么都没发生。
    """

    candidates = callback_base_candidates()
    if (os.environ.get("JOB_CALLBACK_URL") or "").strip():
        return candidates[0]  # 手动指定即信任，不浪费一次探测

    payload = json.dumps(candidates, ensure_ascii=False)
    cmd = (
        f"echo '{_b64(_PROBE_SOURCE)}' | base64 -d > /tmp/_job_probe.py && "
        f"${{PY_BIN:-python3}} /tmp/_job_probe.py '{payload}'"
    )
    try:
        _code, out, _err = await _sbx_bash(cmd, session_id=session_id, user_id=user_id, timeout=60)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"回调地址探测失败（沙箱不可用）: {exc}") from exc

    for line in (out or "").splitlines():
        if line.startswith("PICK "):
            base = line[5:].strip().rstrip("/")
            logger.info("[job] callback base resolved: %s", base)
            return base

    raise RuntimeError(
        "沙箱连不上后端回调地址，作业无法上报进度，已拒绝启动。已尝试："
        + "、".join(candidates)
        + "。请在后端环境变量里设置 JOB_CALLBACK_URL 指向沙箱可达的后端地址"
        "（同一 docker 网络用 http://backend:<端口>，跨网络用宿主映射地址）。"
    )
