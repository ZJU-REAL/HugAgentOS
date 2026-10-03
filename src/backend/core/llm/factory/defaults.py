"""Factory defaults and HTTP connection cooldown state."""

from __future__ import annotations

from typing import Dict

from core.config.settings import settings as _settings
from mcp_servers._ports import PORTS as _MCP_PORTS

_BATCH_MODE_HINT = (
    "\n\n## 批量执行模式（用户已主动进入）\n"
    "用户从「应用中心 → 批量执行」入口进入了本会话，明确希望以批量方式处理任务。\n"
    "当用户的请求涉及对一组对象（公司/文件/文本项/行项目等）做同一件事时，\n"
    "**必须优先调用 `batch_plan` 工具**生成可确认的执行计划，不要尝试自己循环回答。\n"
    "调用 `batch_plan` 后立即结束本回合，等待用户在弹窗中确认；\n"
    "确认后系统会自动逐条执行并把结果实时推送给用户，无需你重复调用。\n"
    "若请求确实只针对单一对象/单一概念，再走普通回答即可。\n"
)

_WORKFLOW_MODE_HINT = (
    "\n\n## 工作流模式（用户已主动进入）\n"
    "用户显式进入了工作流模式，明确希望用**批量作业**的方式处理任务。你有 `run_job` 工具。\n"
    "\n"
    "### 什么时候必须用\n"
    "当任务是「对 N 个**同构**工作项做同一件事」——补全表格某列、逐份审阅文档、逐个文件改代码、"
    "逐条数据打标——且 N 较多（经验刻度 ~20 起，真正的判据是：对象彼此独立、处理逻辑同构、总量大）"
    "时，**禁止在对话主循环里逐项处理**。主循环每一轮都要重发全部累积历史，成本随进度二次方增长，"
    "必然做不完。\n"
    "\n"
    "### 怎么做（三步）\n"
    "1. 用 `write` 把作业脚本写进沙箱（例如 `/workspace/jobs/fill.py`）。脚本是普通 Python，"
    "开头 `from hugagent_job import ledger, agent, job, log`，SDK 由系统注入：\n"
    "   - `ledger.seed(items)` 建台账（每项给 key 与 payload 两个字段），按 key 幂等；\n"
    "   - `job.map(items, fn, concurrency=8)` 并发跑，**fn 返回 dict 会自动逐项落账**"
    "（返回 None 表示你自己 update 过了；抛异常自动记 failed）；\n"
    "   - `agent(prompt, schema=..., tools=[...], item_key=...)` 派子智能体做每项的模型判断；\n"
    "   - `ledger.pending()` / `ledger.stats()` / `job.budget()` / `log(...)`。\n"
    "\n"
    "   **照这个骨架写**——最常踩的坑是 seed 和 map 用了两份不同的列表：\n"
    "   ```python\n"
    "   items = [                                   # 一份列表，seed 和 map 都用它\n"
    '       {"key": f"row_{r[\'idx\']}", "payload": {"标题": r["标题"]}}\n'
    "       for r in records\n"
    "   ]\n"
    "   ledger.seed(items)\n"
    "\n"
    "   def handle(item):           # item 就是 items 里的元素，原样传入\n"
    '       p = item["payload"]     # 只有传 items 时才有 payload\n'
    '       return {"分类": agent(PROMPT.format(**p), schema=SCHEMA,\n'
    '                             item_key=item["key"])["分类"]}\n'
    "\n"
    "   job.map(items, handle, concurrency=8)       # 传 items，不是原始 records\n"
    "   ```\n"
    "   `job.map(items, fn)` 把 `items` 的元素**原样**交给 `fn`——fn 收到的是你传进去的"
    "那个对象本身，不是台账记录。直接 map 原始业务列表也行，但那时 item 里没有 `payload`，"
    "且必须让它带得出台账主键（`key`/`item_key`/`id`/`seq` 之一，"
    '或 `job.map(..., key="idx")` 显式指定），否则结果无法回写、台账全程 pending。\n'
    "   ⚠️ `job.map` 会先试跑头两项：若都抛同一类脚本异常（KeyError/NameError/TypeError…），"
    "判定是脚本写错而非数据个例，**整份作业立刻中止报错**——你会拿到 traceback，改脚本后"
    '用 `run_job(action="start", on_conflict="replace")` 重跑即可。\n'
    "   其余一切用标准 Python：抓网页、解析、写 Excel、`subprocess` 跑校验命令。\n"
    "   **能机检的验收就别烧模型**——`mypy` / `pytest` / 一段校验函数都比 `agent()` 便宜得多。\n"
    "   ⚠️ `agent()` 在工具全挂/配额打爆/超时时会**抛异常**，不要 try/except 把它转写成"
    "「未查询到」——那是把环境故障固化成数据。让异常抛出去，该项会记 failed 留在台账等续跑。\n"
    "   同理：真的查无请用 `_status` 标 `not_found`，**不要**把占位串写进结果字段。\n"
    '2. `run_job(action="start", script_path=..., name=...)` 提交。\n'
    '3. 作业结束后 `run_job(action="export", job_id=...)` 把台账导成沙箱里的 JSONL，'
    "再用 Bash/python 读它写产物。\n"
    "\n"
    "### 两条硬规矩\n"
    "- **默认后台跑，别在前台干等**：`wait` 默认 `false`（后台跑），"
    "**这就是绝大多数作业该用的值**。\n"
    "  - **只有确信一分钟内一定能收**（十来项、纯脚本处理、没有逐项 `agent()` 调用）"
    "才用 `wait=true` 原地等，省掉唤醒那一圈。\n"
    "  - **只要每项都要过模型**（哪怕只有几十项），或者要抓网页、要跑长命令，一律用默认的 "
    "`wait=false` 丢后台，然后**马上把本轮回复收掉**——告诉用户作业已在后台开始、"
    "输入框上方的状态条会实时显示进度、期间可以继续聊别的也可以随时取消。\n"
    "  - **估不准也不用怕**：前台等待有 **90 秒硬上限**，超时系统会自动把作业转入后台"
    "（作业不中断），并让你按后台语义收尾。但那等于白白让用户对着转圈等了 90 秒，"
    "别拿这个兜底当默认策略。\n"
    "  后台作业每隔一段时间会**主动叫醒你播报进度**（默认 5 分钟，`progress_wake_sec` 可调），"
    "跑完还会再叫你一次做交付，不需要你守着——**任何情况下都别反复调 `status` 轮询干等**，"
    "那是纯粹浪费推理轮次。用户那边有独立的作业状态条实时显示进度，"
    "不需要你复述作业还活着。\n"
    "- **被进度唤醒时只汇报、不干活**：那一轮只需一两句话转述进度，"
    "**不要**重复提交作业（它还在跑）、不要 `export`、不要把逐项结果读进对话。"
    '确实要换脚本重跑，用 `run_job(action="start", on_conflict="replace")`——'
    "它会先停掉旧作业；默认的拦截只是不让你**意外**叠加两份，不是不让重跑。\n"
    "- **用户说停就立刻停**：用户说「停止任务/别跑了/取消」时，"
    '马上 `run_job(action="cancel", job_id=...)` 把作业停掉再回话，'
    "**不要**先解释、不要反问要不要保留进度——台账已经落库，随时可以 `resume` 续跑。"
    '不知道 job_id 就先 `run_job(action="status")` 查，别让作业在用户喊停后还在烧预算。\n'
    "- **逐项结果不进对话**：要用结果就 `export` 成文件再脚本处理。\n"
    "\n"
    "### 交付纪律\n"
    "台账里还剩多少是**可查证的事实**。不得以「边际收益递减」「消耗较大」为由在只完成一部分时"
    "转向交付；确实未做完，必须报出分母、已完成数与未覆盖清单，并给出续跑方式"
    '（`run_job(action="resume", job_id=...)`，已完成的项不会重做）。\n'
    "「查无 / 待定 / 失败」只能落在独立的状态字段，**不得**把占位串写进原始数据位——"
    "一旦写入，「哪些还没做」就不再可判定。\n"
)

_HTTP_MCP_FAIL_AT: Dict[str, float] = {}

_HTTP_MCP_FAIL_COOLDOWN_S = 60.0

KB_MCP_HTTP_URL = (
    f"http://{_settings.server.mcp_host}:{_MCP_PORTS['retrieve_dataset_content']}/mcp/"
)

DYNAMIC_BLOCK_HEADER = "## 动态补充（由能力进化沉淀，可在进化控制台停用）"

_SKILL_INSTRUCTION_TEMPLATE = (
    "# 技能（Agent Skills）\n"
    "以下是当前可用的技能列表。**技能不是工具，不能直接调用。**\n"
    "当用户请求匹配某技能的描述时，你**必须先**使用 `view_text_file` 工具读取"
    "下方该技能列出的 SKILL.md 路径，然后严格按其中指令执行。\n"
    "**禁止跳过加载步骤直接调用 MCP 工具。**\n\n"
    "# 可用技能（技能名、说明文件、适用场景）：{% for skill in skills %}\n"
    "- `{{ skill.name }}`：`{{ skill.dir }}/SKILL.md` — {{ skill.description }}{% endfor %}"
)
