# 自动化与计划模式

> 最后更新：2026-10-21

HugAgentOS 内置两种「把一句话变成可重复的生产力」机制，两者都属**社区版（CE）**能力：

| 能力 | 一句话定位 | 后端入口 |
|---|---|---|
| 定时自动化 | 按 cron 周期（或一次性 / 手动）自动执行一段 Prompt 或一个计划 | `features/automation/api/automations.py` + `orchestration/automations/automation_scheduler.py` |
| 计划模式 | AI 先把复杂任务拆成结构化步骤，确认后逐步执行 | `api/routes/v1/plans.py` + `orchestration/plans/` |

## 定时自动化任务

### 桌面混合模式

混合模式下，本机定时任务归属于当前登录的云端账号。系统通过账号映射校验本机项目和会话归属；本机数据库的内部用户 ID 无需与云端相同。创建和管理任务时会检查当前账号及工具授权，退出登录、切换账号或切换云端地址后，旧账号请求不能继续操作本机任务。

### 任务模型

任务存于 `scheduled_tasks` 表（`edition_ee/db/models/automation.py` 的 `ScheduledTask`），核心字段：

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

`orchestration/automations/automation_scheduler.py` 是一个随后端启动的 asyncio 轮询调度器：

- **轮询**：每 15 秒（外加 0–5 秒随机抖动）查询 DB 中 `next_run_at` 到期的任务。
- **分布式锁**：触发前先抢 Redis 锁 `jx:auto:lock:{task_id}`，执行期间持续续期，完成后按持有者令牌释放，防止多实例重复触发。
- **先推进再执行**：触发**之前**先把 `next_run_at` 推进到下一周期——无论本次成败，调度照常前进（与真实 cron 一致），杜绝「卡死的 running 行让 next_run_at 永远停在过去、每次轮询都重新触发」的死循环。
- **执行时长**：单次执行没有固定总时长上限；定时触发与手动执行的 Prompt、Plan、Loop 任务均可持续运行，关联会话及其接续运行也不受普通会话的总时长硬限制。
- **失败治理**：连续失败计数 ≥ `max_failures`（默认 3）时任务自动置为 `disabled`。
- **失联恢复**：启动时及每次轮询检查 `running` 记录。取得该任务的执行锁后，继续跟随已关联的耐久会话并保存最终结果；只有未关联会话的孤儿记录才改为 `failed`，并按需推进过期调度、收尾一次性任务。任务运行多久不影响恢复判断。执行锁每 15 秒续期，60 秒租约用于识别执行者失联，不限制任务总时长。错过的一次性任务同样先取得执行锁。
- **执行锁关闭**：设置停止信号并取消续期，最多等待 2 秒后始终尝试按 token 删除锁；清理失败也移除本地占位并记录错误。执行 Task 结束后续期自动停止。结果投递总等待上限为 60 秒，投递失败不覆盖已提交的成功，也不自动重试。运行记录保存内部所有权 token，完成更新同时校验运行中状态与 token；恢复执行先认领该记录。

每次执行通过统一的 ChatRun 会话执行器创建**真实会话**，并在开跑时立即关联执行记录。指令任务实时展示正文、思考和工具调用；计划任务展示步骤进度。点击运行中的会话或刷新页面，可以跟随和重播与普通会话相同的事件流。用户可在会话中停止执行，指令任务还可通过耐久指令队列追加要求，在下一个安全的模型执行边界生效。主动停止不计入连续失败次数；执行锁失效或调度器关闭会取消后台会话。最终消息、文件、引用和结果投递继续由统一执行器与调度收尾模块负责。会话标题直接使用任务名称，不添加自动化前缀。更新前启动的执行无法补回未记录的实时事件。

定时提示词与普通对话共用会话接收、能力解析、上下文构建和主智能体执行循环。追加要求若由主执行器接续下一轮，定时任务会等待最后一轮的结果再收尾；执行锁失效或关闭调度器会取消当前接续执行。

### 前端

定时任务是独立模块，相关组件位于 src/frontend/src/components/automation/。左侧按任务 ID 合并会话，每个定时任务只显示一条记录；同名的不同任务分别显示。新运行更新入口的最近活动时间，点击入口打开最近一次运行。右侧控制面板保留各次执行记录，选择历史运行后左侧仍选中同一个任务。创建中的会话分别显示，空白创建会话隐藏。侧边栏沿用最早会话作为稳定入口，保留该会话的排序、置顶、收藏和重命名操作。导出和删除作用于当前选中的执行会话，未打开该任务时作用于最近一次执行；删除单次会话不删除定时任务。点击“全部任务”进入内容区管理任务配置、状态和执行记录。通过对话创建时，会先保存归属于定时任务模块的会话，引用已安装的定时任务管理插件并填入可编辑模板。普通对话列表不展示任务会话。

执行会话使用 /automation/:taskId/conversations/:chatId；创建会话使用任务段 new。刷新、前进后退、搜索命中及通知入口均保留任务归属；搜索执行结果直接打开命中的会话，避免误跳到最新执行。RunTimelinePanel 提供按日期分组的执行记录。automationStore 管理唯一任务列表，automationChatStore 仅管理当前执行组和任务置顶、收藏偏好。

执行会话打开右侧 Canvas 时，执行记录面板暂时隐藏，为对话和 Canvas 留出空间。收起 Canvas 后恢复执行记录面板，保留当前任务、选中的执行和会话。

## 计划模式

计划模式把复杂任务拆成结构化步骤再执行，分两阶段（`orchestration/plans/`）：

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
| 自动化 REST API | `src/backend/features/automation/api/automations.py` |
| 调度器（轮询 / 锁 / 恢复） | `src/backend/orchestration/automations/automation_scheduler.py` |
| 自动化服务（cron 计算 / 状态机） | `src/backend/features/automation/service.py` |
| 任务 / 运行记录模型 | `src/backend/features/automation/models.py` |
| 计划 REST API | `src/backend/api/routes/v1/plans.py` |
| 计划生成 / 执行编排 | `src/backend/orchestration/plans/`、`src/backend/core/chat/plans.py` |
| 计划 / 步骤模型 | `src/backend/core/db/models/agent.py` |
| 前端自动化 UI | `src/frontend/src/components/automation/AutomationPanel.tsx` 等、`src/frontend/src/components/automation/RunTimelinePanel.tsx` |
| 前端状态 | `src/frontend/src/stores/automationStore.ts`、`automationChatStore.ts` |

延伸阅读：[对话系统](chat.md) · [MCP 工具](mcp-tools.md) · [数据画布与产物](canvas-artifacts.md)

## 多进程运行与恢复

定时和手动触发共用一个调度循环，每 3 秒扫描一次，最多同时观察 8 个执行。
消费触发信号、推进排期和插入 pending 执行记录在一个数据库事务内完成；
数据库条件更新保护并发接纳。忙碌任务的新手动触发保留至当前执行结束。

pending 表示已接纳、等待执行。调度器停止或租约失效只转交观察权，
不会取消已接纳的会话；后续实例继续观察同一个持久执行及其后继会话。
用户在会话中主动停止仍会停止实际执行。子执行关联和恢复参数与接纳一起提交，
不通过重跑已启动命令恢复结果。运行中的通知投递仍沿用原有投递语义，
此变更不承诺外部渠道的恰好一次投递。

升级数据库须应用 FULL 的 automationqueue01 或 CE 的 ce_0032；
本机 SQLite 使用已安装 automation 模块的 schema 初始化完成约束升级。
存在 pending 记录时拒绝降级，以免丢弃已接纳工作。先停止旧版本调度器、
迁移后启动新版本，不混用两套调度协议。

Plan/Loop 将完整启动参数与子任务接纳一同持久化。接纳后、启动前崩溃时，
后继调度器只领取 pending 子任务；已开始的 running 子任务不会从头重放。
升级前应排空旧版在途任务，旧版未保存的参数不会由兼容路径猜测补齐。

任务投递统一存储为 delivery_targets。升级迁移将旧 channel_id/conversation_id 转换为目标列表并删除旧字段，保留已明确配置的目标和其他元数据；请求处理不再读取旧格式。
