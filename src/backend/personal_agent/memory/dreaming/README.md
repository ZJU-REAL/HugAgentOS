# personal_agent.memory.dreaming

每晚的 Dreaming：用当天的会话和任务记录补上白天漏记的、核实白天在线写下的改动、把整个 wiki 整理一遍。编排写在代码里，其中 analyze 和 organize 两步交给只有文件工具的模型。

## 流程

| 步骤 | 模块 | 做什么 |
| --- | --- | --- |
| 触发 | `runs`、`pipeline.serve`、`wake` | 只有四种：每天用户时区的整点（默认 03:00，没有开关）、一轮没整理完时接着跑（含创世回放）、失败后指数退避重试（最多 `MAX_ATTEMPTS` 次）、用户确认后点“整理一次”。不轮询：`serve` 睡到最早的到期时间（`runs.plan`），“整理一次”和改整理时刻经 Redis 立即唤醒（`wake`），最多每小时兜底醒一次 |
| 租约 | `runs` | 认领时拿 `memory.state` 租约，每次模型调用和工具调用前续租；租约丢了就停下，不回滚别人的写入 |
| 收集 | `collect` / `render` | 队列是 `status == "written"` 且未 dreamed 的日记收据；从数据库渲染成纯文本（USER / AGENT / TOOL CALL / TOOL RESULT，工具结果截到 8 KB，脱敏，不含思考），附上写入日志里这份来源白天的在线改动；单份最多 120000 字符（超过拆开），单次最多 40 份 |
| 快照 | `collect.snapshot` | 第一次写入前给 `memory/` 拍快照（每次尝试一份） |
| Analyze | `agents.analyze` | 每份来源一个 analyzer，串行；Verify → Enrich → Organize；失败重试一次，再失败记进报告，收据留到下次（同一收据失败 3 次后跳过） |
| Organize | `agents.organize` | 一个 organizer：合并重复（留 `MOVED TO` 占位）、标冲突、写 Current picture、归档 90 天无新来源的页面、重建 `_index.md`；总量超过源材料 0.5 倍或 2 MB 时只合并和归档 |
| 收尾 | `finish` | 删除目标存在的占位（CAS）；`lint.check_wiki`；有阻断项或任何异常就恢复快照、收据不动、退避重试；否则写 DREAMS.md 报告、确认收据、释放租约 |

## 模型与工具

- `loop.run` 直接调 AgentScope 的 chat model（`core.models.chat_models.build_model_for_mode`，fast 模式、不流式），不经过 agent factory：Dreaming 不需要技能、MCP、沙箱和审批，只要 5 个自己的文件工具。
- 模型用 `config["dreaming"]["model_provider_id"]`（可选的便宜模型），没有就用智能体自己的 `model_provider_id`，再没有就是平台默认。
- `tools.Toolbox`：`read` / `write` / `edit` / `glob` / `grep`，只能碰 `memory/`；来源是内存里的只读虚拟文件 `sources/<n>.txt`，不写进工作区。写入一律走 `memory.store`，analyzer 带来源 id（writer=ANALYZER），organizer 可以留占位、重写索引（writer=ORGANIZER）。没有删除、网络和 shell。
- 提示词在 `memory/prompts/analyzer.system.md`、`organizer.system.md`，改写自 Anthropic 的 knowledge-wiki 与 memory_distill 示例。

## 不做的事

- 不写 USER.md 和 MEMORY.md：来源里用户说“记住：……”而这两个文件里没有时，只在报告里建议用户自己加。
- 创世回放从数据库里的收据回放（`genesis["since"]` 之后的全部，按时间顺序分批）；`journal/` 里找不到对应消息的记录无法引用来源，只在报告里计数。

## 状态

- 运行行：智能体自己会话里一条 `role=system` 的 ChatMessage，`extra_data["personal_dreaming"]`；`config["dreaming_state"]` 记当前运行、已用掉的整点、累计源材料字节。
- `/dreaming` 返回 `hour, model_provider_id, timezone, next_run, run_id, status, started_at, attempts, next_retry, error, outcomes, completed_at, pending, genesis`。
