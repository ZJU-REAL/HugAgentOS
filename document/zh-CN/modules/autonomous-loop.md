# 自主循环（Autonomous Loop）

> 最后更新：2026-08-13

自主循环让智能体从「一问一答」升级为**能自我推进、跨多次调用、维持外部状态、按可核验目标自主停止的长时运行任务**。它在普通对话（一问一答）与计划模式（线性多步）之外，提供第三种运行形态：一个 run 级的自驱动循环。设计对标 Codex `/goal`（Ralph Loop）：目标跨轮存活、做完为止，同时坚持 maker≠checker——干活的与判卷的永远是两个智能体。

## 核心回路

```
侦察(只读摸清工作区) → 规划(拆需求账本, 可附机检命令) → 每轮:
  worker 跑一轮(全新上下文, 同一持久沙箱, 一次只啃一条需求)
    → 机检(driver 亲自执行 check_cmd, 退出码 0 = 客观达标)
    → 只读评审员亲验真实产出(不采信 worker 自报)
    → 翻牌 / 反馈回灌 / 停滞计数
  → 需求被搁置时对剩余部分重规划 → 全部通过即完成
```

- **侦察式规划**：开工前一个只读侦察员先 `ls`/`read`/`grep` 摸清项目或 /workspace 实况（已有什么、缺什么、有什么坑），规划模型据实拆账本——不再凭目标文本盲拆。纯任务型循环且工作区为空时自动跳过侦察。
- **需求账本（feature_list.json）**：driver 独占，worker 无权增删改；每轮只喂当前一条需求。简单目标允许只拆 1~2 条，复杂目标最多 8 条。
- **混合验收**：能用命令客观判定的需求在规划时附一条只读 `check_cmd`（如 `test -f`、`grep -c`、字数对账），由 **driver 亲自在沙箱执行**——worker 无法作弊；机检未过直接把命令输出回灌下一轮（不消耗一次评审）。语义与质量始终由**只读评审子智能体**打开真实文件核验。
- **二次复核降频**：机检已过的需求单次评审即可翻牌；纯语义需求、以及「翻牌即整环完成」的收官需求，仍需独立二次复核（防提前收工）。
- **状态存磁盘**：每轮迭代全新上下文（避免长会话退化），产物与进度落在持久沙箱（`feature_list.json` / `PROGRESS.md` / `handoffs.md`），账本另有 DB 镜像（重启/换机后以 DB 为准续跑）。
- **运行中重规划**：某条需求连续多轮无实质推进被搁置时，规划器对「剩余未完成部分」重拆（已通过项不动、不可撤销），而不是一条条搁置到「部分完成」收场。重拆次数有护栏（`LOOP_MAX_REPLANS`，默认 2）。

## 停止条件：做完为止，预算是可选项

循环的第一停止条件是**需求账本全部通过**。预算（迭代数/墙钟/token）默认**不设上限**（传 `0` 或不传即不限）——显式传正数仍然生效。防失控的护栏与预算无关，始终在场：

1. **停滞护栏**：一条需求连续 N 轮（编排 Profile 的 `max_attempts_per_requirement`，默认 6）无评审员确证的实质推进 → 搁置（触发重规划 / HITL）。评审员每轮显式判 `progress`——逐章写长文这类"健康推进多轮"不会被误伤。
2. **异常熔断**：worker 单轮抛错/零产出按环境故障处理——不计尝试、退避 30s 重试；连续 `LOOP_MAX_CONSECUTIVE_INFRA`（默认 6）轮才熔断为 `failed`，排除故障后可从断点续跑。
3. **防失控硬后备**：`LOOP_HARD_MAX_ITERS`（默认 500）轮，远超正常任务量级，仅防真正的死循环。

判 `done` 的收官需求会**独立二次复核**，防止误判提前交付。**部分完成收场时自动跑一轮收尾交付**（`LOOP_WRAPUP`，默认开）：整合已完成部分为可用交付物、如实列出未竟事项。

## 终态

| 终态 | 含义 |
|---|---|
| `completed` | 需求账本全部通过（收官二次复核在内） |
| `budget_exhausted` | 部分完成：有需求停滞被搁置且重规划无解，或触及显式预算/硬后备 |
| `cancelled` | 用户取消 |
| `awaiting_human` | 开启 HITL 时，评审请求人工介入 |
| `interrupted` | 服务重启导致中断（启动对账自动归位），可「继续」断点续跑 |
| `failed` | 连续基础设施故障熔断（排障后可续跑） |

## 长跑可靠性（harness）

- **不设年龄硬顶**：自主循环 run 豁免平台统一的运行时长硬顶——进程内还在健康推进的 loop 永不因「跑得久」被回收；孤儿 run 仍按「流静默」判据清理。
- **断点恢复**：账本 DB 镜像 + 持久沙箱双源；服务重启后启动对账把孤儿 loop 归位为 `interrupted`（`LOOP_AUTO_RESUME=true` 时自动续跑）。
- **续跑不丢参**：模型/评审模型/轮数/思考档位随 loop 持久化，崩溃后续跑不会悄悄降级到默认模型。
- **运行中转向（steer）**：无需取消重来——计划条上直接追加指令，driver 下一轮开工前取走并以最高优先级注入 worker。

## 模型配置

- **worker 模型**：跟随用户在会话里选定的模型（与普通聊天同源）。
- **评审/规划模型**：后台「模型管理 → 角色分配 → **自主循环评审与规划**（`loop_reviewer`）」独立配置；未配置回落主智能体模型。侦察、拆解、评审、二次复核、收尾判定共用该角色。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/loops` | 创建循环（`goal_spec`，`budget` 可省=不限） |
| GET | `/v1/loops` / `/v1/loops/{id}` | 列表 / 详情 |
| POST | `/v1/loops/{id}/start` | 启动（SSE 流式跟随） |
| POST | `/v1/loops/{id}/resume` | 续跑（HITL 批准后 / 中断后断点续跑，缺省参数自动回读上次启动参数） |
| POST | `/v1/loops/{id}/steer` | 运行中追加指令（下一轮开工前生效） |
| POST | `/v1/loops/{id}/cancel` | 取消（无活跃任务时直接归位状态） |
| GET | `/v1/loops/{id}/iterations` | 迭代审计轨迹 |

前端入口：**聊天输入框「自主循环」开关**（`components/chat/InputArea.tsx` + `hooks/useLoopMode.ts`），计划条（`components/loop/LoopPlanBar.tsx`）实时显示需求清单、评审状态，并提供「继续」与运行中追加指令。权限由能力位 `can_run_autonomous_loop` 控制（默认开启，可按用户 / 团队关闭）。

## 相关代码

- 驱动器 `orchestration/autonomous_loop.py`、规划器 `orchestration/loop_planner.py`、评审员 `orchestration/subagents/loop_reviewer.py`
- ChatRun 接入 `orchestration/chat_run_executor.py`（`start_autonomous_loop_run` / 启动对账 `resume_running_loops`）
- 服务 `core/services/loop_service.py`（账本镜像 / 启动参数 / steering 队列）、API `api/routes/v1/loops.py`
- 数据表 `agent_loops` / `loop_iterations`
