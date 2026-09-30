"""Standard-library SDK shipped to each job execution."""

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
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

JOB_ID = os.environ.get("JOB_ID", "")
JOB_TOKEN = os.environ.get("JOB_TOKEN", "")
BASE = (os.environ.get("JOB_CALLBACK_URL") or "").rstrip("/")

__all__ = ["ledger", "agent", "job", "log", "JobError"]


class JobError(RuntimeError):
    pass


# 「脚本写错了」而不是「这条数据特殊」的异常。字段名拼错、把两份形状不同的列表搞混、
# 变量没定义——这些在第 1 项就会暴露，且第 N 项必然一模一样地再犯。ValueError 之类
# 常见于正常的数据解析（一行脏数据而已），刻意不列入。
_SCRIPT_BUGS = (
    NameError,
    UnboundLocalError,
    AttributeError,
    TypeError,
    KeyError,
    IndexError,
    ImportError,
)


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

        **预检（fail-fast）**：开跑前先并发试头两项。若它们抛出同一类脚本级异常
        （``KeyError`` / ``NameError`` / ``TypeError`` / ``AttributeError`` …），
        判定是脚本写错而非数据个例，**整份作业立刻中止并抛 JobError**，剩余项一次都不跑。
        异常隔离是为了「一行脏数据别拖垮全批」，不是为了让同一个 bug 安静地重复 N 遍。

        **台账主键怎么取**：默认按 ``key`` → ``item_key`` → ``id`` → ``seq`` 顺序在 item 里找。
        ``ledger.seed`` 用的是 ``{"key": ..., "payload": ...}`` 形状，而 map 常常直接收原始
        业务对象（``{"seq": 7, "name": ...}``）——两者形状不同是常态，所以这里必须兜底，
        否则回写会**静默跳过**（实测：调用真跑了、台账全程 pending、成果全丢）。
        对应地，``fn`` 收到的**就是你传进 map 的那个对象本身**：只有当你传的正是 seed
        那份列表时，``item["payload"]`` 才存在——传原始业务列表就直接读它自己的字段。
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

        def _book(i, out=None, exc=None):
            """把一项的结果/异常落账。预检和并发池共用同一套回写语义。"""
            k = _key_of(items[i])
            if exc is not None:
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
                return
            results[i] = out
            if isinstance(out, dict):
                if k:
                    payload = dict(out)
                    status = payload.pop("_status", "done")
                    ledger.update(k, status=status, result=payload)
                else:
                    _warn_unbookable(items[i])

        # Resume preserves settled output and original input/result ordering.
        settled = {}
        for status in ("done", "not_found"):
            for row in ledger.pending(status=status):
                settled[str(row["key"])] = row.get("result")
        pending_indices = []
        for i, item in enumerate(items):
            k = _key_of(item)
            if k in settled:
                results[i] = settled[k]
            else:
                pending_indices.append(i)
        if not pending_indices:
            return results

        # ── 预检：先拿头两项探路 ────────────────────────────────────────
        # 脚本级 bug（字段名拼错、把 seed 那份列表和原始业务列表搞混）在第 1 项就会
        # 暴露，第 N 项必然一模一样地再犯。旧行为是每项各自记 failed、日志只留一条
        # warn，然后照常打印"全部处理完成"——实测一次 265 项的作业就这么整份跑空，
        # 0 次模型调用、台账全程 pending，而作业状态还是 completed，没人看得出来。
        # 所以：头两项都抛出同一类脚本异常（或总共就一项）即判定脚本写错，整体中止，
        # 把 traceback 抛给运行器 → 作业记 failed → 模型下一轮拿到行号自己改。
        probe_n = min(2, len(pending_indices))
        probe_indices = pending_indices[:probe_n]
        # 两项并发跑（不是串行）：预检是为了早发现 bug，不该给正常作业平白加两次调用的延迟。
        with ThreadPoolExecutor(max_workers=min(n, probe_n)) as probe_pool:
            probe = [probe_pool.submit(fn, items[i]) for i in probe_indices]
        outcomes = []
        for fut in probe:
            try:
                outcomes.append(("ok", fut.result()))
            except BaseException as exc:  # noqa: BLE001 —— 分类交给下面的判定
                outcomes.append(("err", exc))

        bugs = [e for kind, e in outcomes if kind == "err" and isinstance(e, _SCRIPT_BUGS)]
        systematic = len(bugs) == probe_n and (
            probe_n == 1 or type(bugs[0]) is type(bugs[-1])
        )
        for i, (kind, val) in zip(probe_indices, outcomes):
            if kind == "ok":
                _book(i, out=val)
            else:
                if systematic:
                    exc = val
                    raise JobError(
                        "job.map 预检失败：前 %d 项都抛出 %s —— 这是脚本 bug 而不是"
                        "数据个例，作业已整体中止（剩余 %d 项一次都没跑，预算没浪费）。\n"
                        "最常见的原因：传给 job.map 的列表和 ledger.seed 用的不是同一份。"
                        "fn 收到的就是你传进 map 的那个对象本身——只有当你传的正是 seed "
                        "那份 [{'key':…, 'payload':…}] 列表时，item['payload'] 才存在。\n"
                        "%s" % (
                            probe_n,
                            repr(exc)[:200],
                            len(items) - probe_n,
                            "".join(
                                traceback.format_exception(
                                    type(exc), exc, exc.__traceback__
                                )
                            )[-1500:],
                        )
                    ) from exc
                _book(i, exc=val)

        done = probe_n
        rest = pending_indices[probe_n:]
        if rest:
            with ThreadPoolExecutor(max_workers=n) as pool:
                futs = {pool.submit(fn, items[i]): i for i in rest}
                for fut in as_completed(futs):
                    i = futs[fut]
                    try:
                        _book(i, out=fut.result())
                    except Exception as exc:  # noqa: BLE001 —— 异常隔离是本方法的契约
                        _book(i, exc=exc)
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
    return _post("log", {"lifecycle": status, "error": error}, timeout=3)


ledger = _Ledger()
job = _Job()
'''
