"""作业编排运行时（Job Runtime）驱动。

一个 job = 主对话智能体写的一段**作业脚本**的一次执行。脚本跑在沙箱里（普通 Python
进程，拥有文件/网络/并发/子进程），需要模型判断时通过带 job token 的**回调**请求后端
派子智能体——模型凭据因此永远不进沙箱。

驱动职责：

1. 把 SDK（``hugagent_job.py``）+ 用户脚本 + 运行器写进沙箱的 job 目录
2. 以 detached 方式启动运行器（不能同步等：沙箱 HTTP 客户端有 120s 请求超时）
3. 轮询 DB 里的 job 状态直到终态，期间按节流发 ``sub_type=progress``
   —— 这条是喂主 run 无活动看门狗的活性信号，缺了长作业会被当成卡死强杀
4. 到点熔断（墙钟预算）、取消、进程重启后对账

台账在 DB（``job_items``）而不是沙箱文件：沙箱池化复用会让新 job 读到旧 job 的残留账本
（autonomous_loop 踩过这个坑），以 job_id 作主键从结构上避免。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm.attributes import flag_modified

from core.db.engine import SessionLocal
from core.db.models import Job
from core.services.job_service import JobService

logger = logging.getLogger(__name__)

JOB_ROOT = "/workspace/.job"

# 进程内活跃 job 的驱动 task —— 与 chat_run/loop 的做法一致：进程内 task 还活着的 job
# 不按孤儿处理，重启后才由 resume_running_jobs() 对账。
_active_jobs: Dict[str, asyncio.Task] = {}

_POLL_INTERVAL_S = 2.0
_PROGRESS_EVERY_S = 5.0
# 中途唤醒间隔：每次都是一轮真实推理，太密就是烧钱，太疏就等于全程失联。
# 取 5 分钟——15 分钟的盲区太长，作业跑歪了要等一刻钟才有人发现；而播报本身很短，
# 5 分钟一次的上下文成本可以接受。可用 run_job 的 start_params.progress_wake_sec
# 覆盖；<=0 关闭中途唤醒（实时进度看状态条，那条零推理成本）。
_PROGRESS_WAKE_EVERY_S = 300.0
# 无回调静默多久判失联。给得比单次子作业耗时宽裕得多（实测单次可达 210s，还要算上
# 退避重试），但远小于 2 小时墙钟——僵在 running 空烧两小时是最难受的失败形态。
_SILENT_TIMEOUT_S = 900.0
# 孤儿对账（reap_orphan_jobs）：pending 迟迟不转 running 的短闸 + 巡检间隔。
# runner 起来第一件事就是上报 running，5 分钟还没动静基本就是没起来（回调不通/沙箱没了）。
_ORPHAN_PENDING_GRACE_S = 300.0
_ORPHAN_REAP_INTERVAL_S = 120.0


# 探测成功的回调基址（网络拓扑一进程内不会变，探到一次就够）
_resolved_callback_base: Optional[str] = None


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
_PROBE_SOURCE = r'''import json, sys, urllib.request

for base in json.loads(sys.argv[1]):
    try:
        with urllib.request.urlopen(base + "/health", timeout=4) as resp:
            if resp.status < 500:
                print("PICK " + base)
                sys.exit(0)
    except Exception:
        continue
print("NONE")
'''


async def resolve_callback_base(*, session_id: str, user_id: str) -> str:
    """从沙箱里探出一个真正可达的回调基址；一个都不通就抛错（**不许静默启动**）。

    宁可在提交作业这一步就失败——错误会原样回到模型和用户手上，而"启动成功但永远
    没有进度"是最贵的失败形态：驱动干等、状态条转圈、用户等一小时才发现什么都没发生。
    """
    global _resolved_callback_base

    candidates = callback_base_candidates()
    if (os.environ.get("JOB_CALLBACK_URL") or "").strip():
        return candidates[0]  # 手动指定即信任，不浪费一次探测
    if _resolved_callback_base:
        return _resolved_callback_base

    payload = json.dumps(candidates, ensure_ascii=False)
    cmd = (
        f"echo '{_b64(_PROBE_SOURCE)}' | base64 -d > /tmp/_job_probe.py && "
        f"${{PY_BIN:-python3}} /tmp/_job_probe.py '{payload}'"
    )
    try:
        _code, out, _err = await _sbx_bash(
            cmd, session_id=session_id, user_id=user_id, timeout=60
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"回调地址探测失败（沙箱不可用）: {exc}") from exc

    for line in (out or "").splitlines():
        if line.startswith("PICK "):
            base = line[5:].strip().rstrip("/")
            _resolved_callback_base = base
            logger.info("[job] callback base resolved: %s", base)
            return base

    raise RuntimeError(
        "沙箱连不上后端回调地址，作业无法上报进度，已拒绝启动。已尝试："
        + "、".join(candidates)
        + "。请在后端环境变量里设置 JOB_CALLBACK_URL 指向沙箱可达的后端地址"
        "（同一 docker 网络用 http://backend:<端口>，跨网络用宿主映射地址）。"
    )


def callback_base_url() -> str:
    """已探到的回调基址（探测前调用则给候选表里的第一个）。"""
    return _resolved_callback_base or callback_base_candidates()[0]


# ── 沙箱侧 SDK（只依赖标准库；沙箱里不保证有 httpx/requests） ──────────────
SDK_SOURCE = r'''"""hugagent_job —— 作业脚本 SDK（由 Job Runtime 注入沙箱，请勿手工修改）。

暴露三类能力，其余一切（HTTP 抓取、解析、并发、写文件）都用标准 Python 做：

    ledger.seed / pending / update / stats     工作项台账（按业务主键幂等）
    agent(prompt, schema=…, tools=…)           派一个子智能体；凭据在后端，脚本看不到
    job.map / job.budget / log                 并发、预算、进度

断点续跑：重跑同一脚本时 ledger.pending() 自动跳过已完成项，不必重放调用序列。
"""

import json
import os
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

JOB_ID = os.environ.get("JOB_ID", "")
JOB_TOKEN = os.environ.get("JOB_TOKEN", "")
BASE = (os.environ.get("JOB_CALLBACK_URL") or "").rstrip("/")

__all__ = ["ledger", "agent", "job", "log", "JobError"]


class JobError(RuntimeError):
    pass


_warned = set()


def _warn_once(message):
    """同一类问题只喊一次，但一定要喊 —— 沉默是这套东西最贵的失败模式。"""
    if message[:60] in _warned:
        return
    _warned.add(message[:60])
    log("[warn] " + message)


def _post(path, payload, timeout=180):
    url = "%s/v1/internal/jobs/%s/%s" % (BASE, JOB_ID, path)
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Job-Token", JOB_TOKEN)
    last = None
    # 限流要退避到"真的等得起"为止：并发工作项会被同一次 429 同时弹回，
    # 次数太少等于没退避，所以给限流单独一条更长的重试预算。
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
            data = json.loads(raw)
            # 错误一律以 HTTP 4xx/5xx 返回（见后端 HTTPException），这里只拆信封
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            return data
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:400]
            except Exception:
                pass
            # 4xx 是契约问题，重试没有意义 —— 但 **429 除外**：它是限流，是"待会儿再来"，
            # 不是"你写错了"。把 429 当契约错误曾让一次作业整体崩掉：并发回调打爆网关限流后，
            # 连 runner 上报终态的那一发也被 429 拒，作业于是永远停在 running。
            if 400 <= exc.code < 500 and exc.code != 429:
                raise JobError("callback %s -> HTTP %s %s" % (path, exc.code, detail))
            last = exc
        except Exception as exc:
            last = exc
        # 指数退避 + 抖动：等步长退避会让被同一次限流弹回的并发项整齐地再撞一次
        time.sleep(min(1.5 * (2 ** attempt), 30.0) + random.uniform(0, 0.5))
    raise JobError("callback %s failed: %s" % (path, last))


class _Ledger:
    def seed(self, items):
        """items: [{"key": "...", "payload": {...}}, ...]；已存在的 key 一律跳过。"""
        out = {"created": 0, "skipped": 0}
        batch = []
        for it in items:
            batch.append(it)
            if len(batch) >= 500:
                r = _post("ledger", {"op": "seed", "items": batch})
                out["created"] += r.get("created", 0)
                out["skipped"] += r.get("skipped", 0)
                batch = []
        if batch:
            r = _post("ledger", {"op": "seed", "items": batch})
            out["created"] += r.get("created", 0)
            out["skipped"] += r.get("skipped", 0)
        return out

    def pending(self, status="pending", limit=None):
        return _post("ledger", {"op": "pending", "status": status, "limit": limit}) or []

    def update(self, key, status=None, result=None, review=None, error=None, bump_attempts=False):
        out = self._update(key, status, result, review, error, bump_attempts)
        # 后端说这个 key 不在台账里 —— 几乎总是"忘了 ledger.seed"。必须喊出来：
        # 静默打空曾让一次 568 项的作业跑完全程、进度停在 0、成果一条没留下。
        if isinstance(out, dict) and out.get("known_key") is False:
            _warn_once(
                "ledger.update 写了一个台账里不存在的 key=%r —— 是不是漏了 ledger.seed()？"
                "没有台账就没有进度、没有断点续跑，本次回写已被丢弃。" % (key,)
            )
        return out

    def _update(self, key, status, result, review, error, bump_attempts):
        return _post(
            "ledger",
            {
                "op": "update",
                "key": key,
                "status": status,
                "result": result,
                "review": review,
                "error": error,
                "bump_attempts": bool(bump_attempts),
            },
        )

    def stats(self):
        return _post("ledger", {"op": "stats"}) or {}


class _Job:
    def budget(self):
        return _post("ledger", {"op": "budget"}) or {}

    def map(self, items, fn, concurrency=8, key=None):
        """并发跑 fn(item)，**逐项立即落账**。

        回写约定（这样写出来的脚本天然可断点续跑）：

        - fn 返回 dict  → 立刻 ``ledger.update(key, status="done", result=<dict>)``；
          想要别的状态就在 dict 里放 ``_status``（如 ``{"_status": "not_found"}``）。
        - fn 返回 None  → 不自动回写（表示 fn 自己已经 update 过了）。
        - fn 抛异常     → 该项记 failed + error，**不拖垮其余项**。

        千万别写成「先 map 完再统一回写」：那样中途全程 pending，进程一挂全白跑。

        **台账主键怎么取**：默认按 ``key`` → ``item_key`` → ``id`` → ``seq`` 顺序在 item 里找。
        ``ledger.seed`` 用的是 ``{"key": ..., "payload": ...}`` 形状，而 map 常常直接收原始
        业务对象（``{"seq": 7, "name": ...}``）——两者形状不同是常态，所以这里必须兜底，
        否则回写会**静默跳过**（实测：调用真跑了、台账全程 pending、成果全丢）。
        取不到时用 ``key=`` 显式指定字段名或函数；仍取不到则 log 告警，绝不静默。
        """
        items = list(items)
        if not items:
            return []
        n = max(1, min(int(concurrency), 16))
        results = [None] * len(items)
        warned = []

        def _key_of(it):
            if callable(key):
                got = key(it)
                return str(got) if got not in (None, "") else None
            if not isinstance(it, dict):
                return None
            fields = [key] if isinstance(key, str) else ["key", "item_key", "id", "seq"]
            for f in fields:
                got = it.get(f)
                if got not in (None, ""):
                    return str(got)
            return None

        def _warn_unbookable(it):
            sample = list(it.keys())[:8] if isinstance(it, dict) else type(it).__name__
            _warn_once(
                "job.map 取不到台账主键，本次结果无法回写（字段=%s）。"
                "请让 item 带 key/item_key/id/seq，或用 job.map(..., key='字段名')。" % (sample,)
            )

        with ThreadPoolExecutor(max_workers=n) as pool:
            futs = {pool.submit(fn, it): i for i, it in enumerate(items)}
            done = 0
            for fut in as_completed(futs):
                i = futs[fut]
                k = _key_of(items[i])
                try:
                    out = fut.result()
                    results[i] = out
                    if isinstance(out, dict):
                        if k:
                            payload = dict(out)
                            status = payload.pop("_status", "done")
                            ledger.update(k, status=status, result=payload)
                        else:
                            _warn_unbookable(items[i])
                except Exception as exc:  # noqa: BLE001 —— 异常隔离是本方法的契约
                    results[i] = None
                    # 隔离 ≠ 吞掉。事故里 568 项每一项都抛异常，日志上却只有整齐的
                    # "已处理 N/568"，没人看得出一次模型调用都没成功。首个异常必须留痕。
                    _warn_once("job.map 首个失败项：%s" % repr(exc)[:300])
                    if k:
                        try:
                            ledger.update(k, status="failed", error=repr(exc)[:1000],
                                          bump_attempts=True)
                        except Exception:
                            pass
                    else:
                        _warn_unbookable(items[i])
                done += 1
                if done % 10 == 0:
                    log("已处理 %d/%d" % (done, len(items)))
        return results


def agent(prompt, schema=None, tools=(), model=None, timeout=180, max_attempts=2,
          item_key=None):
    """派一个无历史子智能体。schema 非空时强制结构化输出并校验。

    凭据在后端，脚本永远拿不到模型端点或 key。

    **重要**：工具全挂 / 配额打爆 / 连接超时时，本函数**抛 JobError**，不会返回一个
    "查无"的结论——因为那一轮压根没取到证据。请让异常自然向上抛（job.map 会把该项记
    failed 留在台账里等续跑），**不要**用 try/except 把它转写成"未查询到"，那等于把
    环境故障固化成数据。
    """
    return _post(
        "agent",
        {
            "prompt": prompt,
            "schema": schema,
            "tools": list(tools or ()),
            "model": model,
            "max_attempts": int(max_attempts),
            "item_key": item_key,
        },
        timeout=timeout + 60,
    )


def log(message):
    try:
        _post("log", {"message": str(message)[:2000]}, timeout=30)
    except Exception:
        pass
    print("[job] %s" % message, flush=True)


def _lifecycle(status, error=None):
    return _post("log", {"lifecycle": status, "error": error}, timeout=60)


ledger = _Ledger()
job = _Job()
'''


_RUNNER_SOURCE = r'''"""作业运行器 —— 包住用户脚本，上报生命周期。由 Job Runtime 生成。"""

import json
import runpy
import sys
import time
import traceback

import hugagent_job as hj


def _report(status, error=None):
    """上报终态 —— 这一发**绝不允许失败**。

    终态上报失败过一次，代价是作业永远停在 running：驱动只能等墙钟熔断（默认 2 小时），
    期间还在按间隔叫醒智能体报"停滞"。所以这里自己兜住异常并重试；实在报不上去也要
    把状态写进本地文件，让驱动的存活探测能读到真相。
    """
    for _ in range(3):
        try:
            hj._lifecycle(status, error)
            return
        except BaseException:
            time.sleep(3)
    try:
        with open("lifecycle.final", "w") as fh:
            fh.write(json.dumps({"status": status, "error": (error or "")[-2000:]}))
    except BaseException:
        pass


hj._lifecycle("running")
try:
    runpy.run_path("user_script.py", run_name="__main__")
except SystemExit as exc:
    code = exc.code if isinstance(exc.code, int) else 0
    _report("completed" if code == 0 else "failed",
            None if code == 0 else "script exited with code %s" % code)
    sys.exit(0)
except BaseException:
    _report("failed", traceback.format_exc()[-4000:])
    sys.exit(0)
else:
    _report("completed")
'''


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


async def _sbx_bash(command: str, *, session_id: str, user_id: str, timeout: int = 60):
    """在持久沙箱里跑一段 bash，返回 (exit_code, stdout, stderr)。"""
    from core.sandbox import ExecuteRequest, get_sandbox_provider

    provider = get_sandbox_provider()
    res = await provider.execute(
        ExecuteRequest(
            script_content=command,
            script_name="job_ctl.sh",
            language="bash",
            timeout=timeout,
            session_id=session_id,
            user_id=user_id,
        )
    )
    return res.exit_code, (res.stdout or ""), (res.stderr or "")


def _emit_progress(chat_id: Optional[str], note: str) -> None:
    """活性信号：转译成 model_progress 喂主 run 的无活动看门狗。

    长作业期间主对话没有任何可渲染输出，缺了这条会被 600s 看门狗当成卡死强杀。
    """
    if not chat_id:
        return
    try:
        from core.llm import _subagent_stream

        if not _subagent_stream.is_active(chat_id):
            return
        _subagent_stream.push(
            chat_id,
            {"sub_type": "progress", "agent_id": "job", "agent_name": "批量作业", "note": note},
        )
    except Exception:  # noqa: BLE001 —— 活性信号是尽力而为，永远不该拖垮作业
        pass


async def _maybe_wake(job_row_id: str) -> None:
    """作业终态后叫醒会话（幂等；失败只记日志，不影响作业结果）。"""
    try:
        from orchestration.job_wakeup import wake_on_job_finish

        await wake_on_job_finish(job_row_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[job] wake failed job=%s: %s", job_row_id, exc)


async def _maybe_wake_progress(
    job_row_id: str, *, stats: Dict[str, Any], budget_left: Dict[str, Any], stalled: bool
) -> None:
    """中途播报进度（失败只记日志，绝不影响作业本身）。"""
    try:
        from orchestration.job_wakeup import wake_on_job_progress

        await wake_on_job_progress(
            job_row_id, stats=stats, budget_left=budget_left, stalled=stalled
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[job] progress wake failed job=%s: %s", job_row_id, exc)


def _final_from_marker(text: str) -> Tuple[str, str]:
    """从 runner 落盘的 ``lifecycle.final`` 里捡回它没能上报的终态。

    上报走网络会丢，落盘不会——所以进程退出前写的这行 JSON 是最后一手真相。
    捡不到就按 failed 处理：进程没了而作业还 running，本来就不是正常收尾。
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or '"status"' not in line:
            continue
        try:
            data = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        status = str(data.get("status") or "")
        if status in ("completed", "failed", "cancelled"):
            return status, str(data.get("error") or "")
    return "failed", ""


async def _runner_liveness(
    job_row_id: str, *, session_id: str, user_id: str
) -> Tuple[bool, str]:
    """探测沙箱里的 runner 进程是否还活着，并捡回它没能上报的终态。

    为什么必须有：终态上报是一次网络调用，它自己也会失败（实测被网关限流 429 打掉过）。
    一旦丢了，作业就永远 running——驱动干等 2 小时墙钟，期间还在按间隔叫醒智能体报停滞。
    进程存活是**本地事实**，不依赖任何网络，所以拿它当兜底真相。

    返回 (是否还活着, 终态说明)。
    """
    workdir = f"{JOB_ROOT}/{job_row_id}"
    cmd = (
        f"cd {workdir} 2>/dev/null || exit 9; "
        "if pgrep -f '_runner.py' >/dev/null 2>&1; then echo ALIVE; else echo DEAD; fi; "
        "cat lifecycle.final 2>/dev/null; "
        "tail -c 1200 runner.log 2>/dev/null"
    )
    try:
        code, out, _err = await _sbx_bash(
            cmd, session_id=session_id, user_id=user_id, timeout=45
        )
    except Exception as exc:  # noqa: BLE001 —— 探测失败一律当"还活着"，绝不误杀
        logger.warning("[job] liveness probe failed job=%s: %s", job_row_id, exc)
        return True, ""
    if code == 9:
        return True, ""  # 目录还没建好（刚启动），别急着判死
    text = out or ""
    if text.lstrip().startswith("ALIVE"):
        return True, ""
    return False, text[-1200:]


async def _keepalive_sandbox(session_id: str) -> None:
    try:
        from core.sandbox import get_sandbox_provider

        provider = get_sandbox_provider()
        touch = getattr(provider, "touch_session", None)
        if touch:
            await touch(session_id)
    except Exception:  # noqa: BLE001
        pass


async def prepare_and_launch(
    job_row_id: str,
    *,
    user_id: str,
    session_id: str,
    script_text: str,
    token: str,
    interpreter: str = "${PY_BIN:-python3}",
) -> None:
    """把 SDK/用户脚本/运行器写进沙箱并以 detached 方式启动。

    不能同步等脚本跑完：沙箱 HTTP 客户端有 120s 请求超时，长作业必然撞上。

    启动前**先探回调地址**：runner 的一切（建台账、派子作业、上报终态）都走回调，
    回调不通的作业等于没跑，却会以 pending 挂在状态条上转圈。探不到就在这里失败，
    错误直接回到模型/用户手上。
    """
    callback_url = await resolve_callback_base(session_id=session_id, user_id=user_id)
    workdir = f"{JOB_ROOT}/{job_row_id}"
    cmd = "\n".join(
        [
            "set -e",
            f"mkdir -p {workdir}",
            f"cd {workdir}",
            f"echo '{_b64(SDK_SOURCE)}' | base64 -d > hugagent_job.py",
            f"echo '{_b64(_RUNNER_SOURCE)}' | base64 -d > _runner.py",
            f"echo '{_b64(script_text)}' | base64 -d > user_script.py",
            # detached：立即返回，后续状态全部由回调驱动
            "JOB_ID={jid} JOB_TOKEN={tok} JOB_CALLBACK_URL={url} PYTHONUNBUFFERED=1 "
            "nohup {interp} _runner.py > runner.log 2>&1 &".format(
                jid=job_row_id, tok=token, url=callback_url, interp=interpreter
            ),
            "echo launched",
        ]
    )
    code, out, err = await _sbx_bash(cmd, session_id=session_id, user_id=user_id, timeout=90)
    if code != 0:
        raise RuntimeError(f"作业启动失败 exit={code} stderr={(err or out)[:500]}")


async def write_sandbox_file(
    path: str, content: str, *, session_id: str, user_id: str
) -> Tuple[bool, str]:
    """把文本写进沙箱，**分块 + 读回校验**。返回 (是否成功, 说明)。

    为什么不能一条 `echo '<b64>' | base64 -d > f` 了事：沙箱 execute 对命令体积有上限，
    超了之后**静默失败**——exit=0、stderr 为空、文件却不存在（实测拐点在 b64 约
    170KB；568 行台账导出正好落在这个区间，于是"导出成功"但文件从来没出现过）。
    所以这里按块追加，并且以**沙箱里读回的真实字节数**为准，绝不用调用方的计数报成功。
    """
    raw = (content or "").encode("utf-8")
    b64 = base64.b64encode(raw).decode("ascii")
    # 单块 48KB b64（≈36KB 原文），远离静默失败拐点
    chunk = 48_000
    parts = [b64[i : i + chunk] for i in range(0, len(b64), chunk)] or [""]

    code, out, err = await _sbx_bash(
        f"mkdir -p $(dirname {path}) && : > {path}.b64",
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
    # 同样走 base64：沙箱 execute 的 stdout 会丢换行，直接 tail 出来的日志会连成一坨
    code, out, _ = await _sbx_bash(
        f"tail -n {int(tail)} {JOB_ROOT}/{job_row_id}/runner.log 2>/dev/null | base64 -w0 || true",
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


async def drive(job_row_id: str, *, chat_id: Optional[str]) -> Dict[str, Any]:
    """轮询直到 job 进终态；负责墙钟熔断、沙箱保活、活性信号、中途进度唤醒。"""
    last_progress = 0.0
    last_keepalive = 0.0
    last_liveness = time.monotonic()
    started = time.monotonic()
    # 中途唤醒的记账：只在进程内存着——驱动重启时作业本来就要重新接管，多播一次无害，
    # 而写库打标会让每个作业多出一串没人读的 metadata 抖动。
    last_wake = time.monotonic()
    last_wake_settled = -1
    # 心跳 = 累计子作业调用数 + 已结算项数。两者都不动就是真的没在推进。
    last_heartbeat = -1
    last_heartbeat_ts = time.monotonic()

    while True:
        with SessionLocal() as db:
            svc = JobService(db)
            job = svc.get(job_row_id)
            if job is None:
                return {"status": "failed", "error": "job 不存在"}
            status = str(job.status)
            session_id = job.sandbox_session_id or ""
            user_id = job.user_id
            budget = dict(job.budget or {})
            stats = svc.stats(job_row_id)
            wake_every = float(
                dict((job.extra_data or {}).get("start_params") or {}).get(
                    "progress_wake_sec", _PROGRESS_WAKE_EVERY_S
                )
            )
            budget_left = svc.budget_left(job_row_id) if wake_every > 0 else {}

            if status in ("completed", "failed", "cancelled"):
                result = {"status": status, "stats": stats, "error": job.error_message}
                # 终态 → 叫醒会话（仅 wait=False 提交的作业；wait=True 的调用方本来就在等返回）
                await _maybe_wake(job_row_id)
                return result

            elapsed = time.monotonic() - started
            if elapsed > int(budget.get("max_seconds", 7200)):
                svc.finish(job_row_id, "failed", error="超出墙钟预算，作业已熔断")
                return {"status": "failed", "stats": stats, "error": "超出墙钟预算，作业已熔断"}

            # 无回调静默熔断 —— **不依赖沙箱探测**的兜底。
            # 存活探测要经沙箱 provider，而它自己也会坏（实测：跨事件循环的 asyncio.Lock
            # 让探测每次都抛，作业于是继续僵在 running）。回调有没有来是后端自己的事实，
            # 任何外部组件坏掉都不影响这个判断，所以拿它当最后一道闸。
            heartbeat = int(job.usage.get("calls", 0)) if job.usage else 0
            heartbeat += int(stats.get("settled", 0))
            if heartbeat != last_heartbeat:
                last_heartbeat = heartbeat
                last_heartbeat_ts = time.monotonic()
            elif time.monotonic() - last_heartbeat_ts > _SILENT_TIMEOUT_S and elapsed > 120:
                svc.finish(
                    job_row_id,
                    "failed",
                    error=(
                        f"作业静默超过 {int(_SILENT_TIMEOUT_S / 60)} 分钟"
                        "（无子作业调用、台账无推进），判定沙箱或脚本已失联；"
                        "可用 run_job(action='resume') 断点续跑，已完成的项不会重做"
                    ),
                )
                logger.warning("[job] silent timeout, forced failed job=%s", job_row_id)
                await _maybe_wake(job_row_id)
                return {"status": "failed", "stats": stats, "error": "作业静默超时"}

        now = time.monotonic()
        if now - last_progress >= _PROGRESS_EVERY_S:
            last_progress = now
            _emit_progress(chat_id, f"作业进行中 done={stats.get('done')} pending={stats.get('pending')}")
        if session_id and now - last_keepalive >= 60:
            last_keepalive = now
            await _keepalive_sandbox(session_id)
        # 存活探测：runner 没了但作业还挂着 running，说明终态上报丢了，就地补判终态。
        # 60s 起探（给启动留足时间），之后每 90s 一次——比墙钟熔断早两个数量级发现问题。
        if session_id and now - last_liveness >= 90 and now - started >= 60:
            last_liveness = now
            alive, tail = await _runner_liveness(
                job_row_id, session_id=session_id, user_id=user_id
            )
            if not alive:
                final = _final_from_marker(tail)
                with SessionLocal() as db:
                    svc = JobService(db)
                    fresh = svc.stats(job_row_id)
                    svc.finish(
                        job_row_id,
                        final[0],
                        error=final[1] or f"作业进程已退出但未上报终态；runner 日志尾部：{tail[-600:]}",
                    )
                logger.warning("[job] runner gone, forced terminal job=%s -> %s", job_row_id, final[0])
                await _maybe_wake(job_row_id)
                return {"status": final[0], "stats": fresh, "error": final[1]}
        if wake_every > 0 and now - last_wake >= wake_every:
            last_wake = now
            settled = int(stats.get("settled", 0))
            # 第一次播报没有基线，一律当"有进展"处理；之后零增量即判定停滞
            stalled = last_wake_settled >= 0 and settled <= last_wake_settled
            last_wake_settled = settled
            await _maybe_wake_progress(
                job_row_id, stats=stats, budget_left=budget_left, stalled=stalled
            )

        await asyncio.sleep(_POLL_INTERVAL_S)


async def start_job(
    *,
    user_id: str,
    chat_id: Optional[str],
    name: str,
    script_path: str,
    script_text: str,
    session_id: str,
    budget: Optional[Dict[str, Any]] = None,
    start_params: Optional[Dict[str, Any]] = None,
    interpreter: str = "${PY_BIN:-python3}",
) -> str:
    with SessionLocal() as db:
        svc = JobService(db)
        job = svc.create(
            user_id=user_id,
            chat_id=chat_id,
            name=name,
            script_path=script_path,
            script_text=script_text,
            sandbox_session_id=session_id,
            budget=budget,
            start_params={**(start_params or {}), "interpreter": interpreter},
        )
        job_row_id = job.job_id
        token = str((job.extra_data or {}).get("token") or "")

    await prepare_and_launch(
        job_row_id,
        user_id=user_id,
        session_id=session_id,
        script_text=script_text,
        token=token,
        interpreter=interpreter,
    )
    return job_row_id


async def run_and_wait(job_row_id: str, *, chat_id: Optional[str]) -> Dict[str, Any]:
    task = asyncio.create_task(drive(job_row_id, chat_id=chat_id))
    _active_jobs[job_row_id] = task
    try:
        return await task
    finally:
        _active_jobs.pop(job_row_id, None)


def spawn_background(job_row_id: str, *, chat_id: Optional[str]) -> None:
    """wait=False：驱动挂后台 task，主对话立即继续。"""
    if job_row_id in _active_jobs:
        return

    async def _runner():
        try:
            await drive(job_row_id, chat_id=chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[job] driver crashed job=%s: %s", job_row_id, exc)
        finally:
            _active_jobs.pop(job_row_id, None)

    _active_jobs[job_row_id] = asyncio.create_task(_runner())


async def cancel_job(job_row_id: str, *, user_id: str) -> bool:
    """协作式取消：杀沙箱里的运行器进程 + 归位状态（无活跃 task 也不报错）。"""
    with SessionLocal() as db:
        svc = JobService(db)
        job = svc.get(job_row_id)
        if job is None or job.user_id != user_id:
            return False
        session_id = job.sandbox_session_id or ""
        svc.finish(job_row_id, "cancelled", error="用户取消")

    task = _active_jobs.pop(job_row_id, None)
    if task and not task.done():
        task.cancel()
    if session_id:
        await _sbx_bash(
            f"pkill -f '{JOB_ROOT}/{job_row_id}' || true",
            session_id=session_id,
            user_id=user_id,
            timeout=30,
        )
    return True


async def resume_job(job_row_id: str, *, user_id: str, chat_id: Optional[str]) -> Dict[str, Any]:
    """断点续跑：换发 token、原样重启脚本。

    已完成的项留在台账里，脚本侧 ``ledger.pending()`` 自动跳过——不必重放调用序列。
    """
    with SessionLocal() as db:
        svc = JobService(db)
        job = svc.get(job_row_id)
        if job is None or job.user_id != user_id:
            return {"ok": False, "error": "job 不存在或无权访问"}
        if job.status == "running" and job_row_id in _active_jobs:
            return {"ok": False, "error": "作业仍在运行中"}
        script_text = job.script_text or ""
        session_id = job.sandbox_session_id or ""
        interpreter = str((job.extra_data or {}).get("start_params", {}).get("interpreter") or "${PY_BIN:-python3}")
        job.status = "pending"
        job.completed_at = None
        job.error_message = None
        # 续跑后再次终态时要能再叫醒一次
        meta = dict(job.extra_data or {})
        meta.pop("woken_at", None)
        job.extra_data = meta
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(job, "extra_data")
        db.commit()
        token = svc.rotate_token(job_row_id) or ""

    await prepare_and_launch(
        job_row_id,
        user_id=user_id,
        session_id=session_id,
        script_text=script_text,
        token=token,
        interpreter=interpreter,
    )
    return {"ok": True, "job_id": job_row_id}


async def reap_orphan_jobs() -> int:
    """周期性对账：活跃态但**没人在驱动**的 job 判失联，归位 interrupted。

    为什么必须有：``drive()`` 里的所有护栏（墙钟熔断、静默熔断、runner 存活探测）都长在
    驱动协程上——驱动本身没了，护栏也一起没了。而驱动是会没的：``wait=True`` 提交的作业
    驱动挂在工具调用里，用户中止这轮对话、SSE 断掉、run 被回收，驱动就跟着被取消，作业
    则永远停在 pending/running（实测：HugAgentOS 上一条作业 runner 早已死亡，12 分钟后
    DB 里还是 pending、无错误、无台账——状态条只能一直转圈）。启动钩子
    ``resume_running_jobs`` 只在进程重启时兜底，进程没重启就永远兜不到。

    判据用**跨进程可见的证据**（DB ``updated_at``：每次回调写台账/记用量都会推进它），
    而不是只看本进程的 task 表——多 worker 部署里别的进程持有驱动，本进程看不见。
    """
    now = datetime.now(timezone.utc)
    reaped: List[Tuple[str, Optional[str]]] = []
    with SessionLocal() as db:
        rows = db.query(Job).filter(Job.status.in_(("pending", "running"))).all()
        for row in rows:
            job_row_id = str(row.job_id)
            task = _active_jobs.get(job_row_id)
            if task is not None and not task.done():
                continue  # 本进程正在驱动，护栏归 drive() 管
            last = row.updated_at or row.created_at
            if last is None:
                continue
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            quiet = (now - last).total_seconds()
            # pending 用短闸：runner 起来的第一件事就是上报 running，几分钟还没动静就是没起来。
            # running 用与驱动同一把静默闸，避免误杀"单项耗时很长但确实在跑"的作业。
            limit = _ORPHAN_PENDING_GRACE_S if row.status == "pending" else _SILENT_TIMEOUT_S
            if quiet < limit:
                continue
            row.status = "interrupted"
            row.error_message = (
                f"作业已失联（{int(quiet / 60)} 分钟无任何回调，且没有驱动在跟进），"
                "已归位为可续跑状态；run_job(action='resume') 可断点续跑，已完成的项不会重做"
            )
            meta = dict(row.extra_data or {})
            meta.pop("token", None)  # 失联即作废旧 token，防残留进程回来乱写
            row.extra_data = meta
            flag_modified(row, "extra_data")
            reaped.append((job_row_id, row.chat_id))
        if reaped:
            db.commit()

    for job_row_id, _chat_id in reaped:
        logger.warning("[job] orphan job reaped job=%s", job_row_id)
        await _maybe_wake(job_row_id)
    return len(reaped)


async def run_job_reaper_loop() -> None:
    """启动时拉起一次的后台循环；正常情况下永不返回。"""
    while True:
        try:
            await asyncio.sleep(_ORPHAN_REAP_INTERVAL_S)
            await reap_orphan_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("[job] orphan reaper iteration failed", exc_info=True)


async def resume_running_jobs() -> int:
    """进程重启对账：活跃态的 job 全是孤儿（进程内没有任何 driver task），归位 interrupted。

    与 chat_run 的 recover_orphan_runs 一样是启动钩子；区别是 job 的工作项台账在 DB，
    归位后可直接 resume 续跑，已完成的项不会重做。
    """
    with SessionLocal() as db:
        rows = db.query(Job).filter(Job.status.in_(("pending", "running"))).all()
        if not rows:
            return 0
        from sqlalchemy.orm.attributes import flag_modified

        for row in rows:
            row.status = "interrupted"
            row.error_message = "服务重启导致作业中断，可续跑"
            meta = dict(row.extra_data or {})
            meta.pop("token", None)  # 旧 token 一并作废
            row.extra_data = meta
            flag_modified(row, "extra_data")
        db.commit()
        n = len(rows)
    logger.info("[job] orphan jobs recovered count=%d", n)
    return n
