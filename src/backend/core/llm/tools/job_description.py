"""Model-facing run_job usage contract."""

RUN_JOB_DESCRIPTION = r"""提交并管理一次**批量作业**：对 N 个同构工作项做同一件事。

什么时候必须用它：当工作项 ≥ 20 且彼此同构（补全表格某列、逐份审阅文档、
逐个文件改代码、逐条数据打标），**禁止**在对话主循环里逐项处理——那样每一轮都要
重发全部历史，成本随进度二次方增长，做不完。

怎么用（三步）：

1. 用 ``write`` 把作业脚本写到沙箱，例如 ``jobs/fill.py``。
   脚本里 ``from hugagent_job import ledger, agent, job, log`` 即可，SDK 由系统注入：

     - ``ledger.seed([{"key": "r2", "payload": {...}}, ...])`` 建台账（按 key 幂等）
     - ``ledger.pending()`` 取待办；``ledger.update(key, status="done", result=...)`` 回写
     - ``ledger.stats()`` → ``{total, done, pending, settled, remaining, progressed}``
     - ``agent(prompt, schema=..., tools=["internet_search"], item_key=...)`` 派子智能体，
       返回按 schema 校验后的对象；**每项一次判断**用它，多轮工具循环也用它
     - ``job.map(items, fn, concurrency=8)`` 并发跑，单项异常自动隔离并写回 error；
       开跑前先试头两项，若都抛同一类脚本异常（KeyError/NameError/…）判定脚本写错，
       整份作业立刻中止报错，不会把同一个 bug 重复 N 遍
     - ``job.budget()`` → ``{calls_left, tokens_left, seconds_left}``；``log("...")`` 报进度

   **照这个骨架写**（最常踩的坑就是 seed 和 map 用了两份不同的列表）::

       items = [                                  # 一份列表，seed 和 map 都用它
           {"key": f"row_{r['idx']}", "payload": {"标题": r["标题"], ...}}
           for r in records
       ]
       ledger.seed(items)

       def handle(item):          # item 就是 items 里的元素，原样传入
           p = item["payload"]    # 只有传 items 时才有 payload
           return {"分类": agent(PROMPT.format(**p), schema=SCHEMA,
                                 item_key=item["key"])["分类"]}

       job.map(items, handle, concurrency=8)      # 传 items，不是原始 records

   ``job.map(items, fn)`` 把 ``items`` 的元素**原样**交给 ``fn`` —— ``fn`` 收到的
   是你传进去的那个对象本身，不是台账记录。想直接 map 原始业务列表也行，但那时
   ``item`` 里没有 ``payload``，且必须让它带得出台账主键
   （``key``/``item_key``/``id``/``seq`` 之一，或 ``job.map(..., key="idx")``）。

   其余一切用标准 Python：抓网页、解析、写 Excel、``subprocess`` 跑校验命令。
   **验收能机检就别烧模型**——``mypy`` / ``pytest`` / 一段校验函数都比 ``agent()`` 便宜。

2. ``run_job(action="start", script_path="jobs/fill.py", name="补全展品")``。
3. 作业结束后用 ``action="export"`` 把台账导成沙箱里的 JSONL，再用 Bash/python
   读它写产物（Excel、报告、校验）。**不要**把逐项结果读回对话。

两条硬规矩：

- **默认后台跑，别在前台干等，任何情况下都别轮询**。默认 ``wait=False``：
  工具立刻返回 job_id，此时**先把当前这轮回复收掉**——告诉用户作业已在后台开始、
  进度看输入框上方的状态条、随时可以继续聊别的。作业跑完（以及每隔一段时间）
  系统会**自动唤醒本会话**让你播报，不需要你守着。
  ``wait=True`` 只在你确信**一分钟内一定能收**时用（十来项、纯脚本处理、
  没有逐项 ``agent()`` 调用）；只要每项都要过模型，不管多少项都按后台走。
  估不准也不用怕：前台等待有 90 秒硬上限，超时系统会**自动把作业转入后台**
  （作业不中断），并让你按后台语义收尾——但那等于白白让用户干等了 90 秒，
  所以别拿它当默认策略。无论哪种，反复调 ``action="status"`` 干等都纯属浪费轮次。
- **逐项结果不进对话**。要用结果就 ``export`` 成文件再脚本处理。
- **用户喊停就立刻停**。用户说「停止任务 / 别跑了 / 取消」时，先
  ``action="cancel"`` 停掉再回话，不要先解释也不要反问——台账已落库，
  之后 ``action="resume"`` 就能接着跑，停一下不损失任何已完成的工作。

Args:
    action (`str`): ``start`` 提交 / ``status`` 查进度 / ``export`` 导出台账 /
        ``resume`` 断点续跑 / ``cancel`` 取消。
    script_path (`str`): 作业脚本在沙箱里的绝对路径（action=start 必填）。
    name (`str`): 作业名，便于在进度里辨认。
    job_id (`str`): status / resume / cancel 必填。
    wait (`bool`): **默认 false = 后台跑**：立即返回 job_id，作业跑完 / 每隔一段
        时间自动叫醒本会话播报，用户全程能看状态条、能插话、能取消。
        true 则原地阻塞，**仅适用于你确信一分钟内能收的小作业**（十来项、
        纯脚本处理、没有逐项 ``agent()``）。只要每项都要过模型就用默认值——
        阻塞会让会话看起来「卡死在前台」，既看不到进度也没法中途干预。
        兜底：前台最多等 90 秒，超时作业**自动转后台继续跑**（不中断），
        返回 ``{"detached": true}``，你按后台语义收掉这轮即可。拿不准用默认值。
    progress_wake_sec (`int`): 仅 wait=false：每隔多少秒把你叫回来播报一次进度
        （默认 300 秒＝5 分钟；0 表示只在终态叫一次）。被叫醒时只需转述进度，
        别重复提交作业。用户看到的实时进度条不靠它，它只决定你何时该介入。
    on_conflict (`str`): 仅 start，本会话已有在跑作业时怎么办。``block``（默认）
        拦下并把三条出路告诉你；``replace`` 先停掉旧作业再提交新的（换了脚本要重跑
        就用它）；``parallel`` 两份并行（确实互不相干时才用，预算是双份的）。
    interpreter (`str`): 跑脚本的解释器。默认 ``${PY_BIN:-python3}`` —— 沙箱里
        **裸 python3 是干净的系统解释器，连 openpyxl/pandas 都没有**，而 ``$PY_BIN``
        指向预装全套数据依赖的那个。除非有特别理由，**别覆盖这个默认值**；真要装
        额外的包再用 ``uv run --with <pkg> python``（沙箱可出网）。
    max_calls (`int`): 子作业调用次数上限，0 = 用默认。
    max_seconds (`int`): 墙钟秒数上限，0 = 用默认。
    concurrency (`int`): 子作业并发，默认 8，上限 16。
    dest_path (`str`): 仅 export：导出文件路径，默认
        ``jobs/<job_id>_ledger.jsonl``。
    status (`str`): 仅 export：只导出这些状态，逗号分隔（如 ``"done,not_found"``）；
        留空导出全部。

Returns:
    JSON。start/resume 返回 ``{ok, job_id, status, stats}``；status 返回
    ``{ok, status, stats, usage, budget_left}``。``stats.remaining>0`` 表示还没做完，
    必须如实告诉用户并给出续跑方式，不得当作完成。
"""
