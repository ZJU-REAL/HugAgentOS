# Harness v2（4.1–4.9）交付与验证手册

本文档是 4.1–4.9 的代码、迁移、故障恢复和回滚入口。数据库是执行事实的唯一来源；Redis 只承担可丢失、可重建的流式投影。

## 完成矩阵

| 章节 | 生产实现 | 关键保证 | 主要验证 |
| --- | --- | --- | --- |
| 4.1 RunJournal | `core/services/run_journal.py`、`orchestration/chat_run_executor.py` | 接受先落库、租约认领、单调操作序号、恢复快照、旧 owner fencing | `test_run_journal.py`、`test_chat_run_recovery.py`、`test_run_journal_postgres.py` |
| 4.2 ChatSequencer | `core/services/chat_sequencer.py`、`core/db/models/chat.py` | 单 chat 单主写者；消息、预留回复序号和 Run 同事务；不同 chat 互不阻塞 | `test_chat_sequencer.py`、`test_chat_sequencer_migration.py` |
| 4.3 ToolGateway | `core/services/tool_effect_ledger.py`、`core/llm/middlewares.py` | Intent 必须先提交；幂等键；replay/reconcile/never-replay；未知写结果禁止重放 | `test_tool_effect_ledger.py`、`test_tool_effect_migration.py` |
| 4.4 SteerQueue | `core/services/steer_queue.py`、`api/routes/v1/chat_runs.py` | steer/followUp/nextRun 统一持久队列；有序、可重投、ack 幂等、superseded 可审计 | `test_steer_queue.py`、`test_chat_run_steer_api.py` |
| 4.5 CompactionCoordinator | `core/services/compaction_service.py`、`core/services/chat_service.py` | 固定 source watermark、租约、CAS 单赢家、统一预算、失败不伪造成功 | `test_compaction_watermark.py`、`test_compaction_integration.py`、`test_compaction_e2e.py` |
| 4.6 MemoryOutbox/ProfileStore | `core/memory/outbox.py`、`core/memory/profile_store.py` | 先提交 Outbox 再后台执行；租约重试；外部 effect receipt；Profile revision CAS | `test_memory_outbox.py`、`test_profile_cas.py`、`test_memory_retrieval_lifecycle.py` |
| 4.7 PromptManifest | `core/llm/execution_manifest.py`、`core/llm/manifest_agent.py` | 完整内容稳定 hash；只持久化 hash/ref；跨进程可复现；run/workspace 绑定 | `test_execution_manifest.py`、`test_execution_manifest_binding.py` |
| 4.8 Context IR | `core/llm/context_ir.py`、`core/llm/context_adapter.py` | 所有模型上下文先变为 ContextItem；结构化 provenance/trust；确定性预算；工具对不可拆 | `test_context_adapter.py`、`test_execution_surface_snapshot.py` |
| 4.9 Hook/Event/Usage | `core/harness/`、`core/llm/agentscope_hook_adapter.py` | 中立 HookBus；不可变事件；每次物理 model/tool/hook 尝试追加记账；总量派生 | `test_hook_bus.py`、`test_harness_usage_ledger.py`、`test_agentscope_hook_adapter.py` |

跨章节验收位于 `test_harness_e2e_fault_matrix.py` 和 `test_harness_process_restart.py`。后者调用 `scripts/harness_fault_injection.py`，在 `pending`、`model_before`、`model_after`、`tool_intent`、`tool_unknown`、`message_committed`、`compacting`、`memory_outbox` 八个安全点真正发送 `SIGKILL`，然后由新进程恢复同一份数据库。`test_harness_full_migration_chain.py` 则从全新空 SQLite 数据库开始跑完整历史迁移链，并验证 Harness 降级和再升级。

## 迁移顺序

Harness 迁移已经串成一条线性链：

```text
pluginui01
  -> runjrnl01
  -> tooleff01
  -> steerq01
  -> compact01
  -> hookbus01
  -> harness65seq
  -> harness69mem
```

上线时先停止旧写入者，再执行 `python -m alembic upgrade head`，最后启动新版本。SQLite 和 PostgreSQL 都必须从空库完成 `upgrade head`，再完成 `downgrade pluginui01` → `upgrade head` 验证；PostgreSQL 测试需要独占的临时数据库，不能指向共享开发库。历史迁移中的 PostgreSQL 专属 JSONB、INET、类型转换、`now()`、`UPDATE ... FROM` 和直接 ALTER 约束均已改为跨方言写法，PostgreSQL 的 JSONB/INET 语义仍由方言 variant 保留。

## 验证命令

在 `src/backend` 中运行：

```bash
python -m pytest -q \
  tests/services/test_chat_sequencer.py \
  tests/orchestration/test_run_journal.py \
  tests/orchestration/test_chat_run_recovery.py \
  tests/orchestration/test_tool_effect_ledger.py \
  tests/orchestration/test_steer_queue.py \
  tests/llm/test_compaction_watermark.py \
  tests/memory/test_memory_outbox.py \
  tests/memory/test_profile_cas.py \
  tests/llm/test_execution_manifest.py \
  tests/llm/test_context_adapter.py \
  tests/orchestration/test_hook_bus.py \
  tests/orchestration/test_harness_usage_ledger.py \
  tests/orchestration/test_harness_full_migration_chain.py \
  tests/orchestration/test_harness_process_restart.py
```

也可以单独运行真实进程崩溃矩阵；`STATE_DIR` 必须是一个空目录：

```bash
python scripts/harness_fault_injection.py matrix --state-dir "$STATE_DIR"
```

PostgreSQL 验证：

```bash
RUN_JOURNAL_POSTGRES_URL="$PG_URL" \
TEST_POSTGRES_URL="$PG_URL" \
python -m pytest -q \
  tests/orchestration/test_run_journal_postgres.py \
  tests/services/test_chat_sequencer_migration.py \
  tests/memory/test_memory_outbox_migration.py
```

交付时的实测结果：

- 受影响 Harness 回归：448 passed、5 skipped、0 failed。
- 全新 SQLite：完整历史链升级到 `harness69mem`，降级到 `pluginui01` 后再升级成功。
- 全新 PostgreSQL 15.19：同一轮升级、降级、再升级成功，最终版本为 `harness69mem`，public schema 共 113 张表。
- 前端生产构建通过；完整后端回归中的 19 个失败均已在未修改基线复现并归类为既有环境/测试问题，Harness 相关测试无失败。

Redis 的正确性边界是“删光 Redis 后 DB 仍能恢复”。组合测试应先启动隔离 PostgreSQL 和隔离 Redis，再运行 Harness 集成组；故障阶段执行 `FLUSHALL`，并验证 Run、Steer 和事件 offset 仍由数据库恢复。无 Redis daemon 的本地环境使用显式 `REDIS_URL=memory://`，不能把连接失败静默降级成内存 Redis。

## 兼容性

- 原有 SSE 事件类型和主要 payload 保持兼容；offset 现在由数据库单调分配，Redis 丢数据后不会复用。
- 历史消息新增 `chat_seq` 并按 `(created_at, message_id)` 确定性回填；之后排序和删除边界只使用序号。
- ContextItem 的 AgentScope 消息转换集中在 `context_adapter.py`，中立 HookBus 的 AgentScope 执行转换集中在 `agentscope_hook_adapter.py`；Context IR、Hook、Event 和 Usage 核心模块不导入框架类型。
- ToolCallLog 是 ToolEffectLedger 的投影，不能再作为外部副作用是否发生的权威证据。
- 旧 compaction checkpoint 没有 watermark 时会失效并重建，不能继续当作安全摘要使用。

## 回滚

首选“应用回滚、保留新增表”的方式：新迁移以新增字段和新增表为主，保留 schema 能避免丢失 Run、工具 effect、Steer 和 Memory Outbox 证据。

如果必须回滚 schema：

1. 停止 API、后台 worker、compactor 和 memory outbox worker，禁止产生新写入。
2. 确认没有 live Run、processing Outbox 或未决 ToolEffect；未知外部写结果先人工处理。
3. 备份数据库，并导出 `chat_run_operations`、`tool_effect_ledger`、`chat_steer_queue`、`memory_outbox`、`harness_event_log` 和 `harness_usage_attempts`。
4. 使用隔离副本先演练 `python -m alembic downgrade pluginui01`。
5. 只有演练、数据保留和旧应用兼容性都确认后，才在目标环境执行同一降级。

Schema 降级会删除 Harness 账本数据，不可把它当作普通的无损应用回滚。
