"""作业「跑了等于没跑」的第二次事故 —— 265 项分类作业整份跑空却报成功。

现场（trace 00f82fc8 / job_816883c4247c455f）：模型写的脚本把台账建在包装过的
``seed`` 列表上，却把**原始业务列表**交给 ``job.map``::

    seed = [{"key": f"row_{r['idx']}", "payload": {...}} for r in records]
    ledger.seed(seed)

    def classify(item):
        payload = item["payload"]      # ← 期望 seed 那种形状
        ...

    job.map(records, classify, ...)    # ← 传的却是 records（没有 payload、也没有主键）

后果三连，每一环都不报错：

1. 每项进 ``classify`` 第一行就 ``KeyError('payload')``，265 项在 0.27 秒内全挂，
   一次模型调用都没发出去；
2. ``records`` 取不到台账主键，连"失败"都写不回台账 —— 265 项全程 pending；
3. 脚本本身正常退出（异常都被 map 隔离了），于是作业记成 **completed**，
   用户和模型看到的都是"已完成"。

同一次实测还暴露了第四件事：模型自己判断 ``wait=True``，把一个每项都要过模型的
265 项作业当成"小作业"，前台一口气阻塞了 5 分钟。

这里逐条锁住修复：
- ``job.map`` 预检：连续两项抛同一类脚本异常即整体中止，把 traceback 交出去；
- ``JobService.finish``：终态以台账为准，脚本退出码 0 但一项没产出照样判 failed；
- ``run_and_wait``：前台等待有硬上限，超时把作业转后台而不是继续阻塞对话。
"""

import asyncio

import pytest

from orchestration.job_runtime import SDK_SOURCE


# ── ① job.map 预检：脚本级 bug 必须当场中止 ──────────────────────────


@pytest.fixture()
def sdk():
    """exec 一份注入沙箱的真 SDK，把回调层换成记录器。"""
    ns: dict = {}
    exec(compile(SDK_SOURCE, "<sdk>", "exec"), ns)
    calls: list = []

    def fake_post(path, payload, timeout=180):
        calls.append((path, payload))
        if path == "ledger" and payload.get("op") == "seed":
            return {"created": len(payload.get("items") or []), "skipped": 0}
        return {"ok": True, "known_key": True}

    ns["_post"] = fake_post
    ns["_calls"] = calls
    return ns


def _updates(ns):
    return [p for path, p in ns["_calls"] if path == "ledger" and p.get("op") == "update"]


def _logs(ns):
    return [str(p.get("message") or "") for path, p in ns["_calls"] if path == "log"]


def test_map_aborts_when_every_item_hits_the_same_script_bug(sdk):
    """事故原型：seed 一份、map 另一份 → 每项 KeyError('payload')。

    必须整体中止，而不是把同一个错误安静地重复 265 遍再报"全部处理完成"。
    """
    records = [{"idx": i, "标题": f"t{i}"} for i in range(265)]
    seen = []

    def classify(item):
        seen.append(item)
        payload = item["payload"]  # records 里没有这个字段
        return {"分类": payload["标题"]}

    with pytest.raises(Exception) as err:
        sdk["job"].map(records, classify, concurrency=8)

    assert type(err.value).__name__ == "JobError", "中止要用 SDK 自己的 JobError"
    assert len(seen) == 2, "预检只该试两项，剩下 263 项一次都不许跑"

    msg = str(err.value)
    assert "KeyError" in msg, "得把真正的异常带出去"
    assert "payload" in msg and "seed" in msg, "要点名 seed/map 传了两份不同列表这个常见根因"
    assert "Traceback" in msg, "模型需要行号才能自己改脚本"


def test_map_does_not_abort_on_a_single_bad_row(sdk):
    """一行脏数据不该拖垮全批 —— 异常隔离仍是 map 的核心契约。"""

    def fn(item):
        if item["seq"] == 0:
            raise KeyError("这行确实缺字段")
        return {"ok": 1}

    sdk["job"].map([{"seq": i} for i in range(6)], fn, concurrency=2)

    ups = _updates(sdk)
    assert len(ups) == 6, "六项都要落账"
    assert sorted(u["status"] for u in ups) == ["done"] * 5 + ["failed"]


def test_map_does_not_abort_on_non_script_exceptions(sdk):
    """工具 503 / 配额打爆是环境问题，整批全挂也不该判成脚本写错。"""

    def boom(it):
        raise RuntimeError("搜索工具 503")

    sdk["job"].map([{"seq": i} for i in range(5)], boom, concurrency=2)

    ups = _updates(sdk)
    assert len(ups) == 5 and all(u["status"] == "failed" for u in ups)
    assert any("首个失败项" in m for m in _logs(sdk)), "留痕的老契约不能丢"


def test_map_aborts_on_single_item_script_bug(sdk):
    """只有一项时无从佐证，脚本级异常直接判死 —— 失败要响，不要静音。"""

    with pytest.raises(Exception) as err:
        sdk["job"].map([{"seq": 1}], lambda it: it["不存在的字段"])

    assert type(err.value).__name__ == "JobError"


def test_map_tolerates_two_different_bug_types(sdk):
    """只有"同一类异常连着来"才算系统性 —— 两种不同的异常保守放行，避免误杀。"""

    def fn(item):
        if item["seq"] == 0:
            raise KeyError("a")
        if item["seq"] == 1:
            raise TypeError("b")
        return {"ok": 1}

    sdk["job"].map([{"seq": i} for i in range(4)], fn, concurrency=2)
    assert len(_updates(sdk)) == 4


def test_map_runs_every_item_exactly_once(sdk):
    """预检那两项不能在并发池里再跑一遍 —— 重复调用就是重复烧钱。"""
    seen = []
    sdk["job"].map(
        [{"seq": i} for i in range(7)],
        lambda it: (seen.append(it["seq"]), {"ok": 1})[1],
        concurrency=3,
    )
    assert sorted(seen) == list(range(7))


def test_sdk_documents_the_preflight(sdk):
    """SDK 说明是模型唯一看得到的那份，预检行为必须写在里面。"""
    doc = sdk["job"].map.__doc__ or ""
    assert "预检" in doc and "中止" in doc


# ── ② 终态判定：台账说了算，不是退出码说了算 ─────────────────────────


def _mk_job(db, job_id="job_t1"):
    from core.db.models import Job

    job = Job(job_id=job_id, user_id="u1", chat_id="c1", name="t", status="running")
    db.add(job)
    db.commit()
    return job


def _mk_items(db, job_id, statuses):
    from core.db.models import JobItem

    for i, st in enumerate(statuses):
        db.add(JobItem(job_id=job_id, item_key=f"k{i}", status=st))
    db.commit()


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["pending"] * 5, "failed"),                  # 事故原型：一项都没回写
        (["failed"] * 5, "failed"),                   # 每项都抛，同样是白跑
        (["pending", "failed", "pending"], "failed"),
        (["done", "pending", "pending"], "completed"),  # 有产出就不判失败，剩余量另有提示
        (["not_found"] * 3, "completed"),             # 查无是合法结论
        (["needs_review"] * 3, "completed"),          # 有意送审也是合法结论
        ([], "completed"),                            # 压根没建台账的脚本不归这条管
    ],
)
def test_finish_downgrades_completed_when_nothing_was_produced(db_session, statuses, expected):
    from core.services.job_service import JobService

    _mk_job(db_session, "job_x")
    _mk_items(db_session, "job_x", statuses)

    svc = JobService(db_session)
    svc.finish("job_x", "completed")

    job = svc.get("job_x")
    assert job.status == expected
    if expected == "failed":
        assert job.error_message and "无一产出结果" in job.error_message


def test_finish_keeps_explicit_failure_reason(db_session):
    """真正的崩溃原因优先于兜底文案 —— 别把 traceback 覆盖掉。"""
    from core.services.job_service import JobService

    _mk_job(db_session, "job_y")
    _mk_items(db_session, "job_y", ["pending"])

    svc = JobService(db_session)
    svc.finish("job_y", "failed", error="脚本崩了：KeyError('payload')")
    assert svc.get("job_y").error_message == "脚本崩了：KeyError('payload')"


def test_set_wake_on_finish_flips_start_params(db_session):
    """前台转后台后要有人叫醒会话播报，标记必须真的落库。"""
    from core.services.job_service import JobService

    job = _mk_job(db_session, "job_z")
    job.extra_data = {"start_params": {"wake_on_finish": False, "model_name": "m"}}
    db_session.commit()

    svc = JobService(db_session)
    svc.set_wake_on_finish("job_z", True)

    params = svc.get("job_z").extra_data["start_params"]
    assert params["wake_on_finish"] is True
    assert params["model_name"] == "m", "别把同级的其它启动参数冲掉"


# ── ③ 前台等待封顶：超时转后台，不是继续阻塞 ─────────────────────────


@pytest.mark.asyncio
async def test_run_and_wait_detaches_instead_of_blocking_forever(monkeypatch):
    """wait=True 撞上长作业时，等够上限就把对话还给用户，作业继续在后台跑。"""
    from orchestration import job_runtime

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_drive(job_row_id, *, chat_id):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"status": "completed"}

    monkeypatch.setattr(job_runtime, "drive", fake_drive)

    res = await job_runtime.run_and_wait("job_d", chat_id="c1", detach_after=0.05)

    assert res == {"status": "detached"}
    assert started.is_set()
    await asyncio.sleep(0.05)
    assert not cancelled.is_set(), "转后台 ≠ 取消作业，它必须继续跑"
    assert "job_d" in job_runtime._active_jobs, "还在跑就得留在活跃表里，否则重复提交拦不住"

    job_runtime._active_jobs["job_d"].cancel()


@pytest.mark.asyncio
async def test_run_and_wait_returns_result_when_job_finishes_in_time(monkeypatch):
    """作业按时收工时行为不变 —— 封顶不能把正常的前台等待也改坏。"""
    from orchestration import job_runtime

    async def fake_drive(job_row_id, *, chat_id):
        return {"status": "completed", "stats": {"done": 3}}

    monkeypatch.setattr(job_runtime, "drive", fake_drive)

    res = await job_runtime.run_and_wait("job_e", chat_id="c1", detach_after=5)
    assert res["status"] == "completed"
    assert "job_e" not in job_runtime._active_jobs, "收工后要从活跃表摘掉"


def test_tool_caps_foreground_wait():
    """上限得是个真常量，别又退回「让模型自己判断」。"""
    from core.llm.tools.job_tool import _FOREGROUND_WAIT_CAP_S

    assert 30 <= _FOREGROUND_WAIT_CAP_S <= 180


def test_detached_payload_tells_the_model_to_wrap_up(monkeypatch):
    """转后台的返回体是模型唯一的行动依据：得说清「没中断」且「这轮就收掉」。"""
    from core.llm.tools import job_tool

    marked = []
    monkeypatch.setattr(job_tool, "_mark_wake_on_finish", marked.append)

    out = job_tool._detached_payload("job_q")

    assert marked == ["job_q"], "转后台必须打开唤醒标记，否则跑完没人播报"
    assert out["ok"] is True and out["job_id"] == "job_q"
    assert out["waited"] is False and out["detached"] is True
    assert out["status"] == "started", "对模型而言语义就是「作业在后台跑着」"
    assert "没有中断" in out["next"]
    assert "轮询" in out["next"], "别让它转头就去 status 干等"
