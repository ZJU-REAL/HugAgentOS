# 自主循环（Autonomous Loop）

> 最后更新：2026-07-10

自主循环让智能体从「一问一答」升级为**能自我推进、跨多次调用、维持外部状态、按可验证目标自主停止的长时运行任务**。它在普通对话（一问一答）与计划模式（线性多步）之外，提供第三种运行形态：一个 run 级的自驱动循环。

## 核心回路

```
读状态(持久沙箱文件) → 智能体跑一轮(全新上下文, 同一持久沙箱) → 环境验证(verify_cmd)
  → 评估器判 verdict → 反馈回灌 + 压缩交接 → 下一轮
```

每轮迭代拿到全新上下文（避免长会话退化），工作产物与进度落在持久沙箱的文件里（`PROGRESS.md` / `state.json` / `handoffs.md`）——状态存磁盘、不堆在上下文里。

## 两种发起形态

| 形态 | 入口 | 目标与验证 |
|---|---|---|
| **对话模式**（`self_verify`） | 聊天输入框的「自主循环」开关（与「计划模式」并列） | 用自然语言描述目标即可，无需手填任何验证配置。迭代以普通助手消息（markdown 转录）实时回灌到当前会话。 |
| **表单模式**（`verify`） | 实验室模块 → 自主循环 | 显式填 `verify_cmd` / 目标分 / 预算，适合已有确定验证命令的场景（如 EdgeBench 评测）。 |

**对话模式的评估自动二选一**（worker 每轮按目标性质自选，评估器随之路由）：

- **可量化目标**（成本 / 误差 / 通过率…）→ worker **自建** `/workspace/verify.sh`，驱动器独立跑它做**规则评分**（ground truth，不采信 worker 自报）。无显式阈值时靠**停滞收敛**收口：已有合法解且连续多轮无实质提升即判达成——保证真的"反复迭代自我提升"，而非首个合法解就退出。
- **定性目标**（文案 / 方案 / 润色…）→ worker 产出成果 + 证据说明、**不写脚本**；启动时先用一次 LLM 把目标拆成验收标准，之后由**独立 LLM 评估器**（driver 每轮直调、worker 不可调，防自评偏置）按标准逐条判 `done / continue`。

## 退出判定：环境 ground truth 优先

退出条件的可靠性顺序：

1. **环境验证（主）**：`goal_spec.verify_cmd` 在持久沙箱执行，按退出码 + 输出判达成（如测试通过 / 指标达标 / 目标文件存在）。对话模式下这个脚本由 worker 自建、驱动器独立运行。能被环境确定性验证的，不走大模型判断。
2. **评估器（补）**：目标无法完全靠命令验证时，独立评估器读取环境证据 + 验收标准逐条核对，输出二元判定（done / continue / off_track）。评估器与执行体分离、由循环驱动器每轮确定性调用，避免执行体自评的偏置。
3. **预算兜底**：最大迭代数 / 墙钟 / 累计 token 触顶即停（无人值守下的护栏）。

判定 `done` 时会**二次复验**（复跑 verify），防止误判提前交付。

## 三类退出出口

| 终态 | 含义 |
|---|---|
| `completed` | 环境验证达标（含二次复验） |
| `budget_exhausted` | 触及迭代 / 墙钟 / token 预算 |
| `cancelled` | 用户取消 |
| `awaiting_human` | 开启 HITL 时，评估器请求人工（可选） |

## 能力要点

- **动态待办**：智能体每轮在 `state.json` 维护可增删的待办清单，逐项打勾。
- **自我修正**：连续多轮分数无实质提升 → 自动提示「换一个根本不同的策略」重做。
- **断点恢复**：进程重启后，持久沙箱文件仍在 → 从 `state.json` 断点续跑（`LOOP_AUTO_RESUME` 开启）。
- **定时推进**：定时任务支持 `loop` 类型，按 cron 周期推进同一个持久循环（而非每次新建无状态任务）。
- **人工检查点（HITL）**：默认「记录后继续」全自动；per-loop 可选开启，在关键点暂停等人工批准后 `/resume` 续跑。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/loops` | 创建循环（`goal_spec` + `budget`） |
| GET | `/v1/loops` / `/v1/loops/{id}` | 列表 / 详情 |
| POST | `/v1/loops/{id}/start` | 启动（SSE 流式跟随） |
| POST | `/v1/loops/{id}/resume` | 续跑（HITL 批准后 / 崩溃后断点续跑） |
| POST | `/v1/loops/{id}/cancel` | 取消 |
| GET | `/v1/loops/{id}/iterations` | 迭代审计轨迹 |

前端入口：**聊天输入框「自主循环」开关**（对话模式，`components/chat/InputArea.tsx` + `hooks/useLoopMode.ts`）或**实验室模块 → 自主循环**（表单模式，`components/lab/LabPanel.tsx`）。权限由能力位 `can_run_autonomous_loop` 控制（默认开启，可按用户 / 团队关闭）。

## 相关代码

- 驱动器 `orchestration/autonomous_loop.py`、评估器 `orchestration/loop_evaluator.py`
- ChatRun 接入 `orchestration/chat_run_executor.py`（`start_autonomous_loop_run`）
- 服务 `core/services/loop_service.py`、API `api/routes/v1/loops.py`
- 数据表 `agent_loops` / `loop_iterations`
