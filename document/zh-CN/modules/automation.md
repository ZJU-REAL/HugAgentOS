# 自动化与计划模式

> 最后更新：2026-09-30

HugAgentOS 内置两种「把一句话变成可重复的生产力」机制，两者都属**社区版（CE）**能力：

| 能力 | 一句话定位 | 后端入口 |
|---|---|---|
| 定时自动化 | 按 cron 周期（或一次性 / 手动）自动执行一段 Prompt 或一个计划 | `api/routes/v1/automations.py` + `orchestration/schedulers/automation_scheduler.py` |
| 计划模式 | AI 先把复杂任务拆成结构化步骤，确认后逐步执行 | `api/routes/v1/plans.py` + `orchestration/subagents/plan_mode.py` |

## 定时自动化任务

### 桌面混合模式

混合模式下，本机定时任务归属于当前登录的云端账号。系统通过账号映射校验本机项目和会话归属；本机数据库的内部用户 ID 无需与云端相同。创建和管理任务时会检查当前账号及工具授权，退出登录、切换账号或切换云端地址后，旧账号请求不能继续操作本机任务。

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
- **分布式锁**：触发前先抢 Redis 锁 `jx:auto:lock:{task_id}`（TTL 1800 秒），防止多实例重复触发。
- **先推进再执行**：触发**之前**先把 `next_run_at` 推进到下一周期——无论本次成败，调度照常前进（与真实 cron 一致），杜绝「卡死的 running 行让 next_run_at 永远停在过去、每次轮询都重新触发」的死循环。
- **执行超时**：单次执行 wall-clock 上限 1500 秒，严格小于锁 TTL，保证超时先于锁过期。
- **失败治理**：连续失败计数 ≥ `max_failures`（默认 3）时任务自动置为 `disabled`。
- **启动恢复**：进程重启后先把卡在 `running` 超过 40 分钟的孤儿运行记录改为 `failed` 并推进父任务调度；再补发错过的一次性（one-shot）任务。

每次执行通过统一的 ChatRun 会话执行器创建**真实会话**，并在开跑时立即关联执行记录。指令任务实时展示正文、思考和工具调用；计划任务展示步骤进度。点击运行中的会话或刷新页面，可以跟随和重播与普通会话相同的事件流。用户可在会话中停止执行，指令任务还可通过耐久指令队列追加要求，在下一个安全的模型执行边界生效。主动停止不计入连续失败次数；超时会取消后台会话。最终消息、文件、引用和结果投递继续由统一执行器与调度收尾模块负责。会话标题直接使用任务名称，不添加自动化前缀。更新前启动的执行无法补回未记录的实时事件。

定时提示词与普通对话共用会话接收、能力解析、上下文构建和主智能体执行循环。追加要求若由主执行器接续下一轮，定时任务会等待最后一轮的结果再收尾；超时或关闭调度器会取消当前接续执行。

### 前端

定时任务是独立模块，相关组件位于 src/frontend/src/components/automation/。左侧按任务 ID 合并会话，每个定时任务只显示一条记录；同名的不同任务分别显示。新运行更新入口的最近活动时间，点击入口打开最近一次运行。右侧控制面板保留各次执行记录，选择历史运行后左侧仍选中同一个任务。创建中的会话分别显示，空白创建会话隐藏。侧边栏沿用最早会话作为稳定入口，保留该会话的排序、置顶、收藏和重命名操作。导出和删除作用于当前选中的执行会话，未打开该任务时作用于最近一次执行；删除单次会话不删除定时任务。点击“全部任务”进入内容区管理任务配置、状态和执行记录。通过对话创建时，会先保存归属于定时任务模块的会话，引用已安装的定时任务管理插件并填入可编辑模板。普通对话列表不展示任务会话。

执行会话使用 /automation/:taskId/conversations/:chatId；创建会话使用任务段 new。刷新、前进后退、搜索命中及通知入口均保留任务归属；搜索执行结果直接打开命中的会话，避免误跳到最新执行。RunTimelinePanel 提供按日期分组的执行记录。automationStore 管理唯一任务列表，automationChatStore 仅管理当前执行组和任务置顶、收藏偏好。

执行会话打开右侧 Canvas 时，执行记录面板暂时隐藏，为对话和 Canvas 留出空间。收起 Canvas 后恢复执行记录面板，保留当前任务、选中的执行和会话。

## 计划模式

计划模式把复杂任务拆成结构化步骤再执行，分两阶段（`orchestration/subagents/plan_mode.py`）：

1. **生成（Phase 1）**：`POST /v1/plans/generate`（SSE 流式）——AI 分析任务描述，产出含标题、描述、步骤列表的计划草稿；每步可声明 `expected_tools` / `expected_skills` / `expected_agents`（期望用到的工具 / 技能 / 子智能体）。
2. **执行（Phase 2）**：`POST /v1/plans/{plan_id}/execute`（SSE 流式）——逐步顺序执行，每步用独立的 agent；步骤状态、工具调用日志、AI 输出逐步落库。

数据模型（`core/db/models/agent.py`）：`Plan`（状态机 `draft → approved → running → completed/failed/cancelled`）+ `PlanStep`（按 `step_order` 排序，记录 `result_summary` / `tool_calls_log` / `ai_output` / `error_message`）。

REST 接口（`/v1/plans`，`api/routes/v1/plans.py`）：`GET ""` 列表、`GET/PATCH/DELETE /{plan_id}` 详情 / 修改（可在确认前编辑步骤）/ 删除、`POST /{plan_id}/cancel` 取消。计划生成 / 执行的系统提示词支持 DB 版本池管理，文件兜底在 `prompts/prompt_text/plan_mode/plan_mode.system.md`，见 [提示词系统](prompts.md)。

计划还可以被定时化：创建 `task_type=plan` 的自动化任务后，调度器每次触发会把已完成 / 失败的计划重置回 `approved` 并清空步骤状态，从头重跑。

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

每天早上自动产出一条“行业晨报”执行会话。左侧始终显示一个任务入口，右侧可选择不同日期的执行结果；通知出现在铃铛中。

**周报流水线（plan 型自动化）**：先在计划模式里搭好「拉取数据 → 汇总分析 → 导出 Word」三步计划并调通，再创建 `task_type=plan`、`cron_expression="0 17 * * 5"` 的任务，每周五 17 点自动重跑整个计划。

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
| 前端自动化 UI | `src/frontend/src/components/lab/AutomationPanel.tsx` 等、`src/frontend/src/components/automation/RunTimelinePanel.tsx` |
| 前端状态 | `src/frontend/src/stores/automationStore.ts`、`automationChatStore.ts` |

延伸阅读：[对话系统](chat.md) · [MCP 工具](mcp-tools.md) · [数据画布与产物](canvas-artifacts.md)
