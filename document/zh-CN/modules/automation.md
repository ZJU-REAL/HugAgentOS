# 自动化与批量执行

> 最后更新：2026-06-11

HugAgentOS 内置三种「把一句话变成可重复 / 可批量的生产力」机制，三者都属**社区版（CE）**能力：

| 能力 | 一句话定位 | 后端入口 |
|---|---|---|
| 定时自动化 | 按 cron 周期（或一次性 / 手动）自动执行一段 Prompt 或一个计划 | `api/routes/v1/automations.py` + `orchestration/schedulers/automation_scheduler.py` |
| 计划模式 | AI 先把复杂任务拆成结构化步骤，确认后逐步执行 | `api/routes/v1/plans.py` + `orchestration/subagents/plan_mode.py` |
| 批量执行 | 「对一组对象逐个做同一件事」打包成可确认的批量计划 | `api/routes/v1/batch.py` + `orchestration/batch_orchestrator.py` + `mcp_servers/batch_runner_mcp/` |

## 定时自动化任务

### 任务模型

任务存于 `scheduled_tasks` 表（`core/db/models/automation.py` 的 `ScheduledTask`），核心字段：

| 字段 | 说明 |
|---|---|
| `task_type` | `prompt`（执行一段提示词）或 `plan`（执行一个已有计划，须校验 `plan_id` 归属） |
| `cron_expression` | 标准 cron 表达式，创建 / 更新时用 `croniter.is_valid()` 校验 |
| `schedule_type` | `recurring`（周期）/ `once`（一次性）/ `manual`（仅手动触发，永不自动调度） |
| `timezone` | 默认 `Asia/Shanghai`；下次触发时间在该时区计算后转 UTC 存储（`automation_service.py::compute_next_run`） |
| `enabled_mcp_ids` / `enabled_skill_ids` / `enabled_kb_ids` / `enabled_agent_ids` | 限定本任务可用的工具 / 技能 / 知识库 / 子智能体；`None` 表示用默认全集，空列表表示严格禁用 |
| `max_runs` / `max_failures` | 最大运行次数；连续失败阈值（默认 3 次后自动停用） |

### REST 接口（`/v1/automations`）

| 方法 / 路径 | 说明 |
|---|---|
| `POST ""` / `GET ""` / `GET /{task_id}` / `PATCH /{task_id}` / `DELETE /{task_id}` | 任务 CRUD |
| `POST /{task_id}/pause` / `resume` / `trigger` | 暂停 / 恢复 / 手动触发 |
| `POST /{task_id}/activate-sidebar` | 在侧边栏激活该任务的会话分组 |
| `GET /{task_id}/runs` | 运行历史（`scheduled_task_runs` 表，含状态、耗时、结果摘要、关联会话） |
| `GET /notifications/list` / `POST /notifications/read` / `delete` | 自动化结果通知（存 Redis 列表 `jx:notifications:{user_id}`，保留最近 50 条、TTL 7 天） |

### 调度机制（automation_scheduler.py）

`orchestration/schedulers/automation_scheduler.py` 是一个随后端启动的 asyncio 轮询调度器：

- **轮询**：每 15 秒（外加 0–5 秒随机抖动）查询 DB 中 `next_run_at` 到期的任务。
- **分布式锁**：触发前先抢 Redis 锁 `jx:auto:lock:{task_id}`（TTL 900 秒），防止多实例重复触发。
- **先推进再执行**：触发**之前**先把 `next_run_at` 推进到下一周期——无论本次成败，调度照常前进（与真实 cron 一致），杜绝「卡死的 running 行让 next_run_at 永远停在过去、每次轮询都重新触发」的死循环。
- **执行超时**：单次执行 wall-clock 上限 800 秒，严格小于锁 TTL，保证超时先于锁过期。
- **失败治理**：连续失败计数 ≥ `max_failures`（默认 3）时任务自动置为 `disabled`。
- **启动恢复**：进程重启后先把卡在 `running` 超过 30 分钟的孤儿运行记录改为 `failed` 并推进父任务调度；再补发错过的一次性（one-shot）任务。

执行产物是**真实的聊天会话**：prompt 型任务复用主对话工作流 `orchestration/workflow.py::astream_chat_workflow`，完整保留工具调用、引用、产物文件；plan 型任务走 `orchestration/subagents/plan_mode.py::astream_execute_plan` 并写入计划执行快照。会话标题以 `[自动化]` 前缀标记，运行历史里可一键「查看对话」。执行结束后通过 Redis 通知 + 侧边栏激活提醒用户。

### 前端

自动化的管理界面在实验室模块下：`src/frontend/src/components/lab/` 的 `AutomationPanel.tsx`（列表）、`AutomationCreateModal.tsx`（创建，含 cron 配置与能力勾选）、`AutomationCard.tsx`、`AutomationDetailPage.tsx`（详情 + 运行历史）。`src/frontend/src/components/automation/RunTimelinePanel.tsx` 在会话侧呈现按日期分组的运行时间轴。状态由 `stores/automationStore.ts`（任务管理）与 `stores/automationChatStore.ts`（侧边栏会话分组）维护。

## 计划模式

计划模式把复杂任务拆成结构化步骤再执行，分两阶段（`orchestration/subagents/plan_mode.py`）：

1. **生成（Phase 1）**：`POST /v1/plans/generate`（SSE 流式）——AI 分析任务描述，产出含标题、描述、步骤列表的计划草稿；每步可声明 `expected_tools` / `expected_skills` / `expected_agents`（期望用到的工具 / 技能 / 子智能体）。
2. **执行（Phase 2）**：`POST /v1/plans/{plan_id}/execute`（SSE 流式）——逐步顺序执行，每步用独立的 agent；步骤状态、工具调用日志、AI 输出逐步落库。

数据模型（`core/db/models/agent.py`）：`Plan`（状态机 `draft → approved → running → completed/failed/cancelled`）+ `PlanStep`（按 `step_order` 排序，记录 `result_summary` / `tool_calls_log` / `ai_output` / `error_message`）。

REST 接口（`/v1/plans`，`api/routes/v1/plans.py`）：`GET ""` 列表、`GET/PATCH/DELETE /{plan_id}` 详情 / 修改（可在确认前编辑步骤）/ 删除、`POST /{plan_id}/cancel` 取消。计划生成 / 执行的系统提示词支持 DB 版本池管理，文件兜底在 `prompts/prompt_text/plan_mode/plan_mode.system.md`，见 [提示词系统](prompts.md)。

计划还可以被定时化：创建 `task_type=plan` 的自动化任务后，调度器每次触发会把已完成 / 失败的计划重置回 `approved` 并清空步骤状态，从头重跑。

## 批量执行

批量执行解决「对 N 个对象做同一件事」的场景（逐个评价 10 家公司、对 Excel 每一行做分析、逐份提取合同条款），流程分两个阶段：

```
用户消息（含批量意图）
  → LLM 调用 batch_plan 工具（mcp_servers/batch_runner_mcp/server.py）
    → MCP 进程 HTTP 回调 POST /v1/internal/batch/resolve（internal_batch.py，BACKEND_INTERNAL_TOKEN 鉴权）
       · 解析上传文件（xlsx / word）→ 结构化 items
       · 或调 LLM 拆分自然语言枚举 → items
       · 推断默认 prompt 模板（含占位符）并落库 BatchPlan
  → 后端暂停 SSE 流，前端弹出确认对话框（BatchConfirmModal：审阅条目、编辑模板占位符）
  → POST /v1/batch/{plan_id}/confirm 确认
  → GET /v1/batch/{plan_id}/stream（SSE）触发 BatchOrchestrator（Phase 2，确定性执行）
```

### 阶段一：计划生成（LLM 驱动）

`batch_plan` 工具的描述里写死了强触发规则：消息出现「批量 / 分别 / 逐个 / 每一个…」等表达、或一句话枚举 ≥2 个并列对象时，模型**必须**调用该工具而不是自己回答；调用后立即结束回合，等待用户确认。工具返回 `plan_id`、条目总数、前 3 条预览、推断的默认模板与可用占位符。

### 阶段二：确定性执行（BatchOrchestrator）

`orchestration/batch_orchestrator.py` 在用户确认后串行迭代 `plan.items`，**每条 item 起一个全新的 ReActAgent**（禁用 batch_runner 防递归），结果逐条写回 DB：

- **刷新存活**：执行跑在独立的后台 asyncio.Task 中，SSE 客户端断开（刷新页面 / 切 Tab）不中断执行；重连时若任务仍在跑则 tail 增量，已结束则从 DB 回放全部结果。
- **重试与跳过**：单条失败按指数退避重试至 `max_retries`，耗尽后记为 `skipped` 并继续后面的条目。
- **取消**：`POST /{plan_id}/cancel` 置 `cancelled` 标记，编排器在条目边界停止；`POST /{plan_id}/cancel-and-resume`（SSE）则取消批量并删除触发它的助手回合，再以禁用 batch_plan 的方式重新流式回答原消息——用于「其实我不想批量」的一键回退。

计划状态机：`pending → confirmed → running → done / failed / cancelled`（`batch_plans` 表）。前端组件：`src/frontend/src/components/batch/BatchConfirmModal.tsx`（确认 / 模板编辑）与 `BatchProgressPanel.tsx`（逐条进度流），状态在 `stores/batchStore.ts`。

## 典型使用场景

**每天 8 点的行业晨报（prompt 型自动化）**

```json
POST /v1/automations
{
  "task_type": "prompt",
  "name": "行业晨报",
  "prompt": "搜索过去24小时新能源汽车行业的重要新闻，输出带要点的简报",
  "cron_expression": "0 8 * * *",
  "schedule_type": "recurring",
  "enabled_mcp_ids": ["internet_search"]
}
```

每天早上自动产出一条 `[自动化] 行业晨报` 会话，通知出现在铃铛与侧边栏。

**周报流水线（plan 型自动化）**：先在计划模式里搭好「拉取数据 → 汇总分析 → 导出 Word」三步计划并调通，再创建 `task_type=plan`、`cron_expression="0 17 * * 5"` 的任务，每周五 17 点自动重跑整个计划。

**Excel 批量分析（批量执行）**：上传一份 200 行的企业名单 xlsx，输入「对每一行的公司给出经营风险评估」——模型调用 `batch_plan` 生成计划，确认模板「请评估{{公司名称}}的经营风险…」后逐行执行，刷新页面不丢进度，失败行自动重试。

## 相关源码

| 主题 | 路径 |
|---|---|
| 自动化 REST API | `src/backend/api/routes/v1/automations.py` |
| 调度器（轮询 / 锁 / 恢复） | `src/backend/orchestration/schedulers/automation_scheduler.py` |
| 自动化服务（cron 计算 / 状态机） | `src/backend/core/services/automation_service.py` |
| 任务 / 运行记录模型 | `src/backend/core/db/models/automation.py` |
| 计划 REST API | `src/backend/api/routes/v1/plans.py` |
| 计划生成 / 执行编排 | `src/backend/orchestration/subagents/plan_mode.py`、`src/backend/core/services/plan_service.py` |
| 计划 / 步骤模型 | `src/backend/core/db/models/agent.py` |
| 批量 REST + SSE | `src/backend/api/routes/v1/batch.py` |
| 批量内部解析接口 | `src/backend/api/routes/v1/internal_batch.py` |
| batch_plan MCP 工具 | `src/backend/mcp_servers/batch_runner_mcp/server.py`、`_planner.py` |
| 批量编排器 | `src/backend/orchestration/batch_orchestrator.py` |
| 前端自动化 UI | `src/frontend/src/components/lab/AutomationPanel.tsx` 等、`src/frontend/src/components/automation/RunTimelinePanel.tsx` |
| 前端批量 UI | `src/frontend/src/components/batch/`、`src/frontend/src/stores/batchStore.ts` |
| 前端状态 | `src/frontend/src/stores/automationStore.ts`、`automationChatStore.ts` |

延伸阅读：[对话系统](chat.md) · [MCP 工具](mcp-tools.md) · [数据画布与产物](canvas-artifacts.md)
