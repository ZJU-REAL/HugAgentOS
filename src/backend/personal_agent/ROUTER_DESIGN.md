# Personal Agent Router 重构设计：Trigger 与 Turn 分离、批处理

状态：草案，待评审。本文只描述设计，不包含代码改动。总体设计以 [SUPERAGENT_DESIGN.md](SUPERAGENT_DESIGN.md) 为准，本文是其中 §7.1 Router 内核的实现细节。两处修订见那份文档的 §16。

## 0. 结论

现有 front turn 的问题在结构层面，而不是缺少一个开关：**一条 inbox 行 = 一个 Trigger = 一轮 LLM = 一个权限上下文 = 一条回复 = 一个租约**，这六件事绑定在同一个 `ident` 上。
`reply_id(ident)`、`owned(state)`、`turn:<ident>:<count>` 这类工具幂等 key、`current_turn["kind"]` 权限判断、`history(ident)` 截止点、`finish()` 投递，全部依赖这个一对一关系。在它上面硬加批处理（第一版方案用 `decision.batch` JSON 记录批次）可以跑起来，但每一处都要特判「主行 / 被合并行」，复杂度会继续累积。

本设计把它拆开重做：

- **Trigger**：持久化的待处理事件（用户消息、任务结果、会话运行结束、心跳、唤醒）。它只描述「发生了什么」，不持有租约。
- **Turn**：Router 对一个会话车道（lane，即 chat_id）的一次运行。它持有租约，**认领一批 Trigger**，推理过程中还能**继续吸收**新到的 Trigger（steer），最后产出一条或多条分段回复。
- **TriggerKind**：每种 Trigger 的行为集中在一个对象里声明，包括如何进入上下文、能否合并、给本轮带来哪些权限、失败时的兜底、回复如何投递。这样可以消除散落在 `prepare / turn_context / history / fallback / finish / front_tools.turn()` 里的 if/elif 分支。

DSH 原型的两个合并点（推理中 steer 进 nextStep、空闲时一次唤醒处理一批）都变成这个模型的自然结果，不再是特例。

## 1. 现状问题清单

| # | 位置 | 问题 |
|---|---|---|
| P1 | `turn_worker._claim` | 一次只认领一行，积压 N 条就串行跑 N 轮 |
| P2 | `front.history` | 截止到当前行的 `chat_seq`，后面已到达的消息看不见；当前行之前的消息又各自跑过一轮，重复做了无效工作 |
| P3 | `front.Reply` | 回复行在 `prepare` 时创建，`chat_seq` 固定，无法处理推理途中插入的输入（任务侧的 `Transcript` 已经解决过同样的问题，front 侧没有复用） |
| P4 | `front_tools.turn()` | 权限按「这一轮的 kind」判断：event 轮不能改任务、heartbeat 只能建一个任务、只有 unattended 才能 `pa_stay_quiet`。批次里混入不同 kind 后，这个模型无法表达 |
| P5 | `front_tools.pa_task_create` | 附件取自 `state["attachments"]`，也就是单条消息的附件；任务的 goal 链接、IM 投递来源 `origin_of` 也都依赖这一条消息 |
| P6 | kind 分支散落 | `prepare` 的 kind 映射、`turn_context` 的 event 块、`history` 的角色改写、`fallback`、`finish` 的投递、`front_tools` 的工具集，至少 6 处各自判断 `payload.kind` |
| P7 | 生产方各自建 inbox 行 | `message_routing.admit`、`front.announce`、`front.hatch`、`heartbeat.admit`、`session_watch.report_finished` 各自 `db.add(PersonalMessageInbox(...))`，payload 结构靠约定 |
| P8 | 租约放在 inbox 行上 | 一轮如果覆盖多条消息，租约、attempts、owner 都要复制到多行上 |
| P9 | `SteeringHook.before_tool` | 新输入一到就拒绝工具调用。对 Router 来说，已经决定的 `pa_task_create` 会被打回，消息持续涌入时还可能活锁 |

## 2. 领域模型

```
Producer ──enqueue──▶ Trigger(pending)  ── lane = chat_id
                                │
           Scheduler.claim ─────┤ 空闲：一次认领 lane 内可合并的一批
                                ▼
                    Turn(running, lease) ── triggers[]
                                │ 推理中：FrontSteering 在 step 边界吸收新 Trigger
                                ▼
                    ReplyStream → 1..n 个回复分段（按输入边界切分）
                                │
                    Turn.finish：按 TriggerKind 结算每个 Trigger，投递最终分段
```

### 2.1 Trigger（复用并改造 `personal_message_inbox`）

保留表名和主键 `message_id`。每个 Trigger 仍然对应一条 ChatMessage，用户消息是 `user` 行，事件是 `system` 行，这一点不变，历史和 UI 都依赖它。

字段调整：

| 字段 | 变化 |
|---|---|
| `kind` | **新增列**，取值 `message / task_result / session_run / heartbeat / bootstrap`。从 `payload.kind` 提升为列，便于索引和调度 |
| `chat_id` | **新增列**，即 lane。目前要 join ChatMessage 才能拿到 |
| `turn_id` | **新增列**，可为空。被哪个 Turn 认领或吸收 |
| `status` | `pending → claimed → done / failed / quiet / stale`。去掉 `responding / routing` |
| `owner`、`lease_until` | **删除**，租约移到 Turn 上 |
| `decision` | 只保留结算结果：`work_item_ids`、`outcome`、`direct`。删除 `attempts` |

### 2.2 Turn（新表 `personal_front_turns`）

| 字段 | 说明 |
|---|---|
| `id` | `pa_turn_<hex>` |
| `agent_id`、`chat_id` | lane |
| `status` | `running / done / failed` |
| `owner`、`lease_until` | 租约，只在这里 |
| `attempts` | 被中断后重放的次数 |
| `reply_ids` | 回复分段的 ChatMessage id 列表，最后一个是最终回复 |
| `created_at`、`finished_at` | |

约束：同一 agent 同时最多一个 `running` 的 Turn，用部分唯一索引或 claim 时锁住 agent 行来保证，沿用现有的按 agent 串行语义，避免 memory 文件、任务操作并发写。未来如果要按 lane 并行，只需要把约束从 agent 换成 chat_id，见 §9。

### 2.3 TriggerKind（新模块 `conversation/router/kinds.py`）

```python
class TriggerKind(Protocol):
    name: str
    batchable: bool                 # 能否与同 lane 其他 Trigger 合并
    wakes: bool                     # False：只在已有 Turn 中被吸收，不单独开轮（DSH 的 inject）
    def render(db, trigger, *, current: bool) -> ModelInput      # 在历史 / 本轮输入中的样子
    def context(db, trigger) -> str | None                        # 本轮 turn_context 追加块（如 event 规则）
    def grants(db, trigger) -> Grants                             # 给本轮带来的权限
    def fallback(db, trigger) -> str | None                       # 本轮失败或无输出时的兜底文本
    def settle(db, trigger, turn, reply_row, failed) -> None      # 结算：链接、投递、记忆
```

各 kind 对照现有代码的出处：

| kind | batchable | wakes | grants | fallback | settle | 现有出处 |
|---|---|---|---|---|---|---|
| `message` | ✔ | ✔ | `change_tasks`、附件、focus 任务 | 已触达任务的状态行 | 关联任务、`mark_for_delivery(reply_to=)`、占位符、记忆 | `admit`、`finish` 的 message 分支 |
| `task_result` | ✔ | ✔ | `quiet`（仅 unattended） | `task.result` | `link(report)`、投递（unattended 投到 home） | `announce`、`finish` 的 event 分支 |
| `session_run` | ✔ | ✔ | 无 | 事件原文 | 同上 | `session_watch.report_finished` |
| `heartbeat` | ✗ | ✔ | `create_one_task`、`quiet`、只读工具集 | 无（失败时安静） | `beat_outcome`、home 投递 | `heartbeat.admit`、`beat_outcome` |
| `bootstrap` | ✗ | ✔ | 全部 | 无（失败时安静） | 无 | `hatch` |

`heartbeat` 和 `bootstrap` 不参与合并。它们的工具集和安静语义与其他 kind 冲突，而且 `heartbeat.check` 本来就只在没有 open Trigger 时才触发，合并的收益几乎为零。

### 2.4 Grants：权限按批次计算，归属按 Trigger

本轮的权限是批次内所有 Trigger 的 `grants` 的并集，有两条额外规则：

- **`quiet`**：只有当批次中**每个** Trigger 都允许安静时才可用。有用户消息就必须回复。
- **工具集**：由批次决定。批次里只有 heartbeat 时用只读工具集，其余情况用完整工具集。prompt 缓存只取决于这两种工具集，不会碎片化。

**归属**解决 P5。`pa_task_create`、`pa_task_update` 增加一个可选参数 `from_messages: list[int]`，填本轮输入里消息的**序号**（不暴露内部 id）：
- 省略时，归属于本轮所有用户消息，这是现在单条消息的等价行为；
- 附件取这些消息附件的并集；
- goal 链接到第一条消息，IM 来源 `origin_of` 天然一致，因为同一 lane 就是同一 IM 会话；
- 工具幂等 key 从 `turn:<ident>:<count>` 改为 `turn:<turn_id>:<count>`。重试时 Turn 不变，key 也不变。

## 3. 调度（替换 `turn_worker.py`）

`conversation/router/scheduler.py`：

```
loop:
  while active < PARALLEL_OWNERS:
    turn = claim(owner)          # 一个事务
    if not turn: break
    spawn run(turn)
  sleep(IDLE)

claim(owner):
  for agent with pending wakes-Trigger or expired running Turn, order by oldest pending:
    lock agent row
    if agent has running Turn with live lease: continue
    if agent has running Turn with expired lease:      # 接管
        turn.attempts += 1; turn.owner = owner; renew; return turn   # 它的 Trigger 原样保留
    head = oldest pending Trigger with wakes (user messages first, then the rest; heartbeat last as now)
    if head.kind is heartbeat and stale: mark stale; continue
    batch = [head] + (head.batchable ? 同 lane 后续 pending 且 batchable 的 Trigger，
                      遇到同 lane 不可合并的就停，上限 MAX_BATCH 条 / MAX_BATCH_CHARS 字符 : [])
    create Turn(running, owner, lease); batch.status = claimed; batch.turn_id = turn.id
    return turn
```

- 租约续期只针对 Turn 一行，P8 消失。
- 「用户消息优先于心跳」和「stale 心跳丢弃」这两条规则保留，判断依据从 `request_key` 前缀改为 `kind` 列。
- `wakes=False` 的 Trigger 不会触发 claim，只会被已经在跑的 Turn 吸收，或者被下一个 Turn 顺带认领。

## 4. 运行一轮（替换 `front.run_turn / prepare / converse / finish`）

`conversation/router/turn.py`。`Turn` 对象取代 `current_turn` 字典，`current_turn` ContextVar 改为持有这个对象。

### 4.1 输入构造

- **历史**：`history(lane, upto=批次最大 chat_seq)`。批次中的 Trigger 按 `kind.render(current=True)` 渲染，不截断；更早的 Trigger 按 `current=False` 截断渲染。现有的心跳隐藏、bootstrap、Dreaming 过滤规则都移到对应的 kind 里。
- **turn_context**：时钟、任务列表、会话状态这些公共块保留。focus 改为批次中所有 Trigger focus 的并集。各 kind 的 `context()` 块去重后追加，例如 `front_event` 规则只出现一次。
- **多条输入的标注**：批次中有多条时，每条前面加 `[#n 时间 来源]` 前缀，供工具的 `from_messages` 引用。只有一条时不加前缀，保持现有行为，缓存前缀也不变。

### 4.2 推理中吸收：`FrontSteering`

- **挂载**：给 front profile 注册 `steering=`，对应 [extension.py](extension.py) 里 front 的 `AgentProfile`，返回 `current_turn.get().steering.hook_policy()`。
- **`take()`**：锁 agent 行，取同 lane 中 `pending` 且 batchable 的 Trigger（包括 `wakes=False` 的），受上限约束，改为 `claimed` 并写上 `turn_id`。返回按 4.1 规则渲染好的文本。
- **`acknowledge()`**：把这些 Trigger 并入 `turn.triggers`，重新计算 Grants，并调用 `reply.boundary()`，见 §5。
- **上限**：每轮最多吸收 3 次或 20 条。超出的留给下一轮，保证一轮能结束。
- **`SteeringHook` 小改**：构造参数增加 `interrupt_tools: bool = True` 和 `render: Callable | None = None`，front 传 `interrupt_tools=False`，解决 P9。注入的文字不再是「用户追加的新指令」，改为「回复前同一会话又收到的新输入」，由 `render` 提供。

如果事务提交了、但模型调用前进程挂了：这些 Trigger 已经是 `claimed`，Turn 重放时历史截止点会包含它们，不会丢。

### 4.3 结算 `finish(failed)`

在一个事务里完成：
1. 校验 Turn 仍归本 owner 所有。
2. **决定最终文本**：
   - 允许安静且模型调用了 `pa_stay_quiet` 时，删除空分段，所有 Trigger 标 `quiet`；
   - 否则取最终分段的文本。为空或失败时，拼接各 Trigger 的 `fallback()`，规则同现状：只有 event 时用 event 兜底，否则在文本后追加 FAILED。
3. **逐个 Trigger 调用 `kind.settle()`**，负责任务链接、`mark_for_delivery`、占位符撤回、记忆写入。最终分段只投递一次：
   - `reply_to` 指向批次中最后一条用户消息，接管它的 IM 占位符；
   - 其余用户消息的占位符走 `mark_placeholder_recall`；
   - 批次中没有用户消息、只有 unattended 结果时，按现有规则投到 home。
4. **状态**：所有 Trigger 标为 `done` 或 `failed`，Turn 标为 `done` 或 `failed`。

**重放**：`attempts > MAX_ATTEMPTS`（心跳为 1）时直接 `finish(failed=True)`。`reprocess(message_id)` 把这条 Trigger 重置为 `pending`、清空 `turn_id`；同一 Turn 中失败的其他 Trigger 一起重置，由下一轮重新认领成一批。

## 5. 回复分段（与任务 Transcript 合并实现）

`tasks/task_threads.Transcript` 已经实现了「输入到达后新开一行、之前的输出留在旧行、未完成的工具结果写回原行」，只是分段条件写死为「chat 中最新一条 user 行的 seq」，P3 在 front 侧缺的就是这个。

**做法**：把这段逻辑抽到 `core/chat/segmented_record.py`（或 `personal_agent/conversation/segments.py`），实现为 `SegmentedRecord(StreamRecord)`，由调用方注入三个 callable：
- `owns(db) -> bool`：Transcript 检查任务 run 和租约，front 检查 Turn 租约；
- `boundary_seq(db) -> int`：Transcript 取最新 user 行，front 取**已吸收**的最大 Trigger seq（未吸收的不触发分段）；
- `open_segment(db) -> ChatMessage`：新建分段行。

**Router 的 `ReplyStream`** 基于它实现：
- 非最终分段标记 `extra.front_intermediate = True`，不投递，保留 `model_steps`，后续历史会回放其中的工具调用，这样顺序是 `A → [工具] → B → 最终回复`。
- 没有内容的分段在 finish 时删除。
- heartbeat 的「完成前隐藏」标记沿用现状。

这样任务和 Router 共用一份分段代码，任务侧的行为不变，现有的 transcript 排序测试负责回归。

## 6. 生产方统一入口

`conversation/router/triggers.py`：

```python
def enqueue(db, agent, *, kind, chat_id, message: ChatMessage, request_key, payload) -> Trigger
```

`admit`、`announce`、`hatch`、`heartbeat.admit`、`session_watch.report_finished` 全部改为调用它（解决 P7）。幂等仍然依靠 `(agent_id, request_key)` 唯一约束。`message_routing.direct()` 路径不变：明确的任务操作当场执行，直接写一条 `done` 的 Trigger，不经过 Router。

## 7. 模块布局

```
conversation/
  router/
    __init__.py
    kinds.py        TriggerKind 协议 + 5 个实现（从 front.py / heartbeat / session_watch 收拢）
    triggers.py     enqueue、inbox_view、pending、reprocess（取代 message_routing 中的相关部分）
    scheduler.py    claim / renew / loop（取代 turn_worker.py）
    turn.py         Turn、Grants、run、finish（取代 front.run_turn/prepare/finish/fallback/beat_outcome）
    context.py      system_prompt、turn_context、history（从 front.py 迁出，逻辑基本不变）
    steering.py     FrontSteering
    reply.py        ReplyStream（基于 SegmentedRecord）
  message_routing.py  只保留 admit（输入校验 + direct）与 apply_direct/apply_approval
  front.py            删除；announce/hatch 移到各自 producer 旁或 router/triggers.py
tools/front_tools.py  turn()/checked()/changing() 改为 Turn 的方法；工具新增 from_messages
```

## 8. 迁移

完整版与 CE 两条 alembic 链各加一个迁移（参考 `personalsteer01` / `ce_0029` 的成对做法）：

1. 给 `personal_message_inbox` 加 `kind`、`chat_id`、`turn_id`，回填：
   - `kind` 来自 `payload.kind`，缺省为 `message`；
   - `chat_id` 来自 join ChatMessage。
2. 新建 `personal_front_turns` 表。
3. 状态迁移：
   - `responding / routing` 的行改回 `pending`，清空 owner 和 lease，交给新调度器重新认领；
   - `attempts` 丢弃。
4. 删除 `owner`、`lease_until` 列。
5. 部署要求和 `personalsteer01` 一样：先停 worker 再升级。降级时重建列、不恢复租约。

## 9. 不在本次范围内，但结构上已留出口

- **按 lane 并行**：Turn 唯一约束从 agent 级改为 chat 级即可。需要先确认 memory 文件写入和任务操作在并发下是安全的，所以暂时不做。
- **非唤醒通知**（`wakes=False`）：机制已经支持，但 v1 不改变任何现有生产方的行为。候选场景：被丢弃的任务（outcome=dropped）的结果、`session_run` 中任务仍在运行的情况。由产品决定后，在对应 kind 上改一个属性即可。
- **一次工具调用多项操作**：类似 DSH 的 `orchestrator_route`。模型本来就能在一步里并行发出多个工具调用，v1 不加。

## 10. 验收与测试

新增 `tests/test_personal_agent_router_batching.py`，`execute` 用 stub 计数模型调用：

| 用例 | 期望 |
|---|---|
| 空闲时同 lane 连发 5 条 | 1 个 Turn，5 个 Trigger 一起 done，1 条最终回复，`reply_to` = 第 5 条 |
| 运行中同 lane 到达 5 条 | 被吸收进同一轮，只多 1 次模型调用；历史顺序是 A → 中间分段 → B..F → 最终分段 |
| 不同 lane 的消息 | 不合并，按到达顺序各开一轮 |
| heartbeat 排在消息后面 | 不合并，消息优先，stale 照旧丢弃 |
| 批次含 unattended 结果 + 用户消息 | `pa_stay_quiet` 被拒绝，可以改任务 |
| 只有 unattended 结果 | 可以安静；所有 Trigger 标 quiet，回复行被删除 |
| `from_messages` | 附件、goal 链接归属到指定消息 |
| 租约过期 | 新 owner 接管同一个 Turn，attempts+1，Trigger 不变，工具 key 幂等 |
| 超过最大重试 | 整批 failed；reprocess 一条，整批重新认领 |
| 吸收上限 | 超出的留在 pending，下一轮认领 |
| `interrupt_tools=False` | 吸收新消息时，进行中的 `pa_task_create` 不会被打回 |
| IM 占位符 | 最后一条被编辑成回复，其余被撤回 |

需要改写的现有测试：`test_personal_agent_routing.py`（租约和 attempts 断言改到 Turn 上）、`test_personal_agent_heartbeat.py`（inbox 查询改用 `kind` 列）、`test_personal_agent_im_actions.py`、`test_personal_agent_session_tool.py`，以及 `run_personal_agent_*_e2e.py` 中直接读写 inbox 字段的部分。

## 11. 实施顺序

1. **核心重构，行为不变**：迁移、`kinds.py`、`triggers.enqueue`、Turn 表加调度器，批大小固定为 1。全部现有测试通过，证明重构本身没有改变行为。
2. **SegmentedRecord 抽取**：Transcript 改用它，任务侧回归测试通过。
3. **空闲批量认领**：打开 MAX_BATCH，加 Grants、`from_messages`，以及多条输入的标注和结算。
4. **推理中吸收**：FrontSteering、SteeringHook 参数、ReplyStream 分段。
5. **Prompt**：`front_rules` 增加多条输入的规则，包括逐条判断、合成一条回复、不同任务分段说明，以及 `from_messages` 的用法。

每一步都可以单独合并。第 1、2 步是纯重构，风险集中在第 3、4 步。
