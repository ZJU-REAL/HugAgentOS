# 记忆系统（mem0）

> 最后更新：2026-08-24

HugAgentOS 内置一套**分层持久记忆系统**：L2 向量层使用 [mem0](https://github.com/mem0ai/mem0) + Milvus，L3 图谱层由本地适配器直连 Neo4j。开启后，智能体能跨会话记住用户的身份背景、偏好习惯、**这里做事的方式**（口径、顺序、红线），以及稳定的实体关系。记忆按信息稳定性分为三层（L1 档案 / L2 做法沉淀 / L3 知识图谱），三层均属社区版能力；只有**记忆审计**（合规留痕）属于商业版（商业版 EE）。

整套系统遵循一条核心承诺：**所有记忆 I/O 绝不在 SSE 主链路上同步等待**——检索走带预算超时的后台任务，写入走 SSE 关闭后的有界后置流水线（见 `src/backend/core/memory/__init__.py` 模块文档）。

## 分层模型

| 层 | 名称 | 存储 | 注入时机 | 实现 |
|---|---|---|---|---|
| L1 | Profile 用户档案 | DB（bounded markdown，默认上限 1500 字符） | 会话启动时冻结注入 | `core/memory/profile.py` |
| L2 | Procedural 做法沉淀 | Milvus 向量库（collection `hugagent_memories`） | 会话启动时按相似度检索 Top-K 注入 | `core/memory/service.py`（mem0 封装） |
| L3 | Graph 图谱记忆 | Neo4j（可选，`MEM0_GRAPH_ENABLED=true`） | 按实体按需检索 | `core/memory/graph.py`（本地 Neo4j 适配器） |
| — | Session 辅助层 | `chats.metadata.session_memory` | 单会话内 | 会话任务工作集 |
| — | Audit 审计旁路（商业版 EE） | DB 表 `memory_audit` | 所有读写旁路记录 | `core/memory/audit.py` |

## 数据流

```
用户发送消息
  │
  ▼
api/routes/v1/chats.py
  · 从 users_shadow.metadata 读 memory_enabled / memory_write_enabled
  · 项目对话则改读 projects.metadata（团队项目 scope = "team:<team_id>"）
  │
  ▼
orchestration/workflow.py
  ├─► launch_memory_retrieval()            ← 后台 task，立即返回（不阻塞）
  │     └─ core/memory/service.retrieve_memories()
  │          ├─ mem0.Memory.search() → Milvus 向量检索
  │          └─ core/memory/graph.py → Neo4j 实体关系检索（可选）
  │
  ├─► build_frozen_memory_block()          ← 组装"会话冻结块"
  │     · L1 Profile：读 DB，<20ms，必等
  │     · L2 Procedure：await 检索 task，预算 600ms（MEMORY_RETRIEVAL_BUDGET_MS）
  │       超时则 shield 后台 task、跳过本轮注入；task 继续完成且状态可观测
  │
  ├─► inject_frozen_memory()               ← 冻结块以 user-role 消息插到
  │                                           session_messages 开头
  │     （用 user 而非 system：Qwen 等模型要求 system 仅在 index 0）
  │
  ▼  …… Agent 流式执行，SSE 输出 ……
  │
  ▼  SSE 关闭后（用户不等待）
save_memories_background()
  └─ core/memory/pipeline.schedule_post_response_tasks()
       · 返回前同步提交 MemoryOutbox pipeline 行
       · 带租约 worker 消费；重启后恢复 pending/retry，失败重试或隔离
       · 全局 Semaphore 限 worker 并发（默认 8）
       · 当前轮分类 + 有界近期轨迹（最多 8 条消息）→ 跑 0~5 个 extractor
       · “失败 → 用户给出不同做法 → 执行成功”保留为 procedural 候选
       · 每个 extractor 单独 30s 超时
       · sanitize 脱敏闸门 → 写 L1/L2/L3/Session → audit 旁路
```

检索与注入的整合层在 `src/backend/orchestration/memory_integration.py`；mem0 的 LLM / Embedder / Milvus / Reranker 配置在 `src/backend/core/memory/service.py`，Neo4j 图谱读写在 `src/backend/core/memory/graph.py`。模型配置优先取 DB 中 `memory` / `embedding` 角色，缺省回落到环境变量。

## 写入流水线与抽取器

写入只在用户显式开启 `memory_write_enabled` 时发生（第一道门在 `save_memories_background()`，第二道门在 `schedule_post_response_tasks()` 内）。流水线（`core/memory/pipeline.py`）特性：

- **持久接纳**：`schedule_post_response_tasks()` 是同步函数，返回前先提交数据库 Outbox；低延迟路径只唤醒生命周期统一管理的 worker，进程重启后由启动 worker 恢复；
- **租约与幂等**：Outbox 状态为 `pending/processing/succeeded/retry/quarantined`，以 `message_id + layer + candidate_hash` 去重；L2/L3 把 candidate id 写成 effect receipt。L2 恢复使用 Strong 一致性的精确 JSON receipt 查询，不受 `top_k` 截断影响；同一作用域的 L2 外部写按数据库 advisory lane 排序，同一候选的多条规则在本地合并 receipt。崩溃重放不会再次新增或强化；lease 过期可接管，失败按退避时间重试；
- **原子检查点与结算**：抽取结果和全部 candidate 子任务在一个事务中形成检查点，pipeline 重放不会再次调用抽取模型；进化卡片 summary 与 settlement Outbox 成功回执也在一个事务提交，不会出现“卡片已改但任务仍会重放”的半完成状态；
- **统一长期写入口**：对话后置写入、Profile 压缩以及交互式 L1/L2 修改都先提交 Outbox，再触碰长期存储；
- **有界并发**：全局 `asyncio.Semaphore`（`MEMORY_BG_MAX_CONCURRENCY`，默认 8）；
- **Milvus 熔断器**：连续失败 N 次（默认 3）后短路 60 秒，检索 / 写入路径共用（`milvus_breaker`）；
- **抽取器路由**（`core/memory/extractors/router.py`）：`identity`（身份）与 `preference`（偏好）的关键词线索负责兜住过短消息，实质轮次也会进入候选；`task`（任务）仍按明确任务线索触发；**`procedural`（做法）不设关键词闸门**，只要本轮实质（用户 ≥8 字且助手 ≥30 字）就跑——一条约定用什么措辞说出来是不可预测的，正则在模型读到之前就替它决定"这轮没有做法"，漏掉的部分既无声也不可见。空集则直接跳过所有 LLM 调用。
- **多轮纠错保留**（`core/memory/trajectory.py`）：后置流水线从当前会话读取最多 8 条近期消息；“助手明确失败 → 用户改变方法 → 后续成功”和“助手曾给出结果 → 用户明确纠正先前做法 → 后续成功”两类轨迹都会确定性保留 `procedural` 候选。门卫模型不能把这类已经由结果验证的经验清空；通用做法抽取仍返回空时，仅对该稀有信号追加一次聚焦失败恢复抽取。普通重试、助手单方面自称的“教训”和没有成功证据的建议不会触发。
- **门卫故障不等于批准**：用户明确要求“记住”和已验证纠正无需 LLM 门卫批准；其他候选遇到门卫超时或乱码时进入 Outbox 重试，达到上限后 `quarantined`，不会失败放行扩大写入。
- **Profile CAS 与 effect receipt**：L1 每次提交递增 `revision`；普通更新和压缩都按 revision 比较后写入，压缩发生冲突时重读最新内容并重新压缩，不能覆盖并发新增字段；同一 Outbox effect 的结果随 Profile 原子保存，崩溃恢复不会再次修改或再次调用压缩模型。
- **L2 只存做法，不存事实**：事实写下来的那一刻就开始过期，记住它等于让系统自信地复述一个陈旧数字，而不是去查当前值；能被重复使用、且能进一步编译成技能的，只有"这里怎么做事"。因此没有 fact 抽取器，也没有回退路径。
- **L3 只存稳定关系，不存步骤**：图谱抽取器只接受用户明确陈述或确认的隶属、负责、依赖、使用、组成、别名、分类等实体关系；快速变化的数值、状态、新闻以及一次性指令均拒绝入图。相同关系再次出现时增加 `seen_count` 并刷新 `last_seen_at`，不会复制边。
- **写入不经 mem0 二次推断**：写入统一带 `infer=False`。mem0 默认会用它自己的通用事实抽取 prompt 再判一次，把我们已经蒸馏好的规则悄悄丢掉——表现为"写入成功但什么都没写"，日志无错、卡片无内容。

> mem0ai 2.x 已从开源 `Memory` 类移除 graph store。项目不再向 mem0 传入会被忽略的 `graph_store` 配置，而由 `core/memory/graph.py` 使用既有 `neo4j` 驱动完成 L3 的抽取后写入、查询、去重强化和删除。

## 脱敏闸门（sanitizer）

所有待写入的记忆内容先经过 `core/memory/sanitizer.py::sanitize()`：

| 类别 | 行为 | 内置规则示例 |
|---|---|---|
| `CLASSIFIED_TERMS` 涉密词 | **拒写**（reject，整条不入库） | 机密 / 秘密 / 绝密 / 内部资料 / Confidential / NDA 等 |
| `REDACT_PATTERNS` 脱敏正则 | 替换为 `[REDACTED:<name>]` 后仍写入 | 身份证、手机号、邮箱、银行卡、API key、JWT、红头文件号、客户编号、内网 URL |

规则支持运行时扩展：DB 表 `memory_sanitizer_rules`（ORM：`core/db/models/memory.py::MemorySanitizerRule`）可追加 / 禁用规则，`rule_type` 取 `redact` / `classified` / `disable_redact` / `disable_classified`，带 5 分钟 TTL 缓存，管理端变更后调 `invalidate_rules_cache()` 立即生效。DB 不可用时静默回落到硬编码规则。

## 记忆审计（商业版 EE）

`core/memory/audit.py` 把所有 L1/L2/L3/session 层的读写操作旁路写入 `memory_audit` 表：

- 记录 actor、action（`read/write/update/delete/write_rejected/forget`）、layer、workspace、chat、密级；
- **原文永不落审计表**——只存 SHA256 `content_hash`；
- 失败不冒泡（审计不阻塞主流程）；
- 开关：`MEMORY_AUDIT_ENABLED`（默认 `true`）。

按 [版本说明](../editions/overview.md)，记忆审计是商业版能力位（`edition_ee/licensing/features.py::Feature.MEMORY_AUDIT`）。审计查询接口为 `GET /v1/memories/audit`（支持按 action / layer 过滤）。

## 记忆管理 API

路由文件：`src/backend/api/routes/v1/memories.py`（注册在 CE 路由表中）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/memories` | L2 做法沉淀列表；`?project_id=` 按项目 workspace 过滤 |
| PATCH | `/v1/memories/{id}` | 通过 Outbox 改写单条 L2 记忆正文（保留 id 与元数据；可传 `operation_id` 幂等重试） |
| PATCH | `/v1/memories/profile/field` | 通过 Outbox 改写 L1 档案单字段（可传 `operation_id` 幂等重试） |
| DELETE | `/v1/memories/profile/field` | 删除 L1 档案的单个字段 |
| GET | `/v1/memories/profile` | L1 用户档案（markdown 全文 + 字符上限） |
| GET | `/v1/memories/graph` | L3 图谱关系列表；支持 `?project_id=` 项目作用域 |
| DELETE | `/v1/memories/graph/{relation_id}` | 删除单条 L3 图谱关系 |
| GET | `/v1/memories/audit` | 审计记录（商业版 EE） |
| GET | `/v1/memories/settings` | 读用户记忆 / 重排开关 |
| PATCH | `/v1/memories/settings` | 更新开关（持久化到 `users_shadow.metadata`） |
| DELETE | `/v1/memories` | 清空当前用户全部 L2 记忆 |
| DELETE | `/v1/memories/{id}` | 删除单条 L2 记忆 |

## 用户开关与作用域

两个独立开关，均存放在 `users_shadow.metadata`（ORM 列名 `extra_data`）：

| 开关 | 含义 | 默认 |
|---|---|---|
| `memory_enabled` | 永久记忆**读取**：会话启动时是否注入冻结块 | `false` |
| `memory_write_enabled` | **写入**：对话结束后是否抽取并保存记忆 | `false` |

项目对话有独立作用域：个人项目 / 默认空间用真实 `user_id`，团队项目用 `scope_user_id = "team:<team_id>"`——同团队成员写入同一个 mem0 桶实现共享，真实作者保留在 `metadata.author_user_id`（见 `orchestration/memory_integration.py::save_memories_background` 与 `api/routes/v1/memories.py::list_memories`）。项目级开关存于 `projects.metadata`（`memory_enabled` / `memory_write_enabled`，项目内缺省 `true`），详见 [项目空间与我的空间](./projects-myspace.md)。

## 前端记忆中心

- 入口：设置弹窗「记忆设置」分区（`src/frontend/src/components/settings/SettingsModal.tsx`），提供「写入记忆」「永久记忆」两个 Switch；
- 「我的分层记忆」弹窗：三个 Tab——档案 L1（markdown 全文）、做法 L2（列表 + 单条编辑/删除 + 一键清空，组件 `src/frontend/src/components/memory/FactsList.tsx`）、图谱 L3（未启用时提示需配置 `MEM0_GRAPH_ENABLED` + Neo4j）；
- 项目维度的记忆查看：`src/frontend/src/components/projects/ProjectMemoriesModal.tsx`；
- API 封装：`src/frontend/src/api.ts`（`getMemories` / `getMemoryProfile` / `getMemoryGraph` / `getMemorySettings` 等）。

## 基础设施

L2/L3 依赖的向量库与图数据库通过 Docker Compose `mem0` profile 一键启动（不启用时主应用零开销短路）：

```bash
docker-compose --profile mem0 up -d
```

| 服务 | 镜像 | 作用 |
|---|---|---|
| milvus | `milvusdb/milvus:v2.5.4`（standalone） | L2 向量存储 |
| etcd | `quay.io/coreos/etcd:v3.5.5` | Milvus 元数据 |
| minio | `minio/minio` | Milvus 对象存储 |
| neo4j | `neo4j:5.15-community` | L3 图谱存储（可选） |

详见 [Docker Compose 部署](../deployment/docker-compose.md)。

## 环境变量

```bash
# 总开关
MEM0_ENABLED=true                 # 默认 false；false 时所有记忆代码路径零开销短路
MEM0_GRAPH_ENABLED=false          # L3 图谱（需 Neo4j）

# Embedding 服务（记忆向量）
MEM0_EMBED_URL=http://<embed-host>/v1
MEM0_EMBED_MODEL=qwen3_embedding_8b
MEM0_EMBED_API_KEY=sk-...
MEM0_EMBED_DIMS=1024

# 记忆抽取用 LLM（缺省回落 MODEL_URL / API_KEY / BASE_MODEL_NAME）
MEMORY_MODEL_NAME=...
MEMORY_MODEL_URL=...
MEMORY_API_KEY=...

# 存储
MILVUS_URL=http://milvus:19530
MILVUS_TOKEN=
NEO4J_URL=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=...

# 行为调优（均有合理默认值）
MEMORY_LAYERED_ENABLED=true       # 分层记忆
MEMORY_AUDIT_ENABLED=true         # 审计旁路（商业版 EE）
MEMORY_RETRIEVAL_BUDGET_MS=600    # 检索预算
MEMORY_BG_MAX_CONCURRENCY=8       # 后台写入并发
MEMORY_OUTBOX_LEASE_SECONDS=120   # worker 租约
MEMORY_OUTBOX_MAX_ATTEMPTS=5      # 超过后隔离
MEMORY_OUTBOX_RETRY_BASE_SECONDS=5 # 指数退避基数
MEMORY_OUTBOX_POLL_SECONDS=1      # worker 轮询间隔
MEMORY_EXTRACT_TIMEOUT_S=30       # 单 extractor 超时
MEMORY_PROFILE_MAX_CHARS=1500     # L1 档案字符上限
MEMORY_FACT_DEFAULT_TTL_DAYS=180  # 兼容旧配置；L2 做法由专用 TTL 控制
MEMORY_FROZEN_TOPK=5              # 冻结块 Procedure Top-K
MEMORY_BREAKER_THRESHOLD=3        # Milvus 熔断阈值
MEMORY_BREAKER_COOLDOWN_S=60      # 熔断冷却

# 可选：检索重排
RERANKER_URL=...
RERANKER_MODEL=...
RERANKER_API_KEY=...
```

完整清单见 [环境变量参考](../deployment/environment-variables.md)。设置定义在 `src/backend/core/config/settings.py::MemorySettings`。

## 相关源码

| 路径 | 职责 |
|---|---|
| `src/backend/core/memory/__init__.py` | 分层记忆包入口与公共 API |
| `src/backend/core/memory/service.py` | mem0 配置组装与异步封装（Milvus / Reranker）+ L3 检索合流 |
| `src/backend/core/memory/graph.py` | L3 Neo4j 图谱关系的写入、强化、查询与删除 |
| `src/backend/core/memory/profile.py` | L1 档案：get / patch / compact / delete |
| `src/backend/core/memory/profile_store.py` | L1 revision CAS 与 effect receipt 存储 |
| `src/backend/core/memory/outbox.py` | 持久接纳、租约、重试/隔离、抽取检查点与原子结算 |
| `src/backend/core/memory/effect_lane.py` | PostgreSQL advisory lock / SQLite 本地锁驱动的 L2 顺序写 lane |
| `src/backend/core/memory/executor.py` | 等待真实线程 effect 完成后再传播取消，避免提前释放租约 |
| `src/backend/core/memory/pipeline.py` | Outbox 接纳入口、信号量、Milvus 熔断器 |
| `src/backend/core/memory/trajectory.py` | 有界近期会话轨迹与“失败—改法—成功”纠错检测 |
| `src/backend/core/memory/extractors/` | identity / preference / task / procedural / graph 抽取器 + 路由 |
| `src/backend/core/memory/sanitizer.py` | 脱敏闸门（硬编码规则 + DB 动态规则） |
| `src/backend/core/memory/audit.py` | 审计旁路（商业版 EE） |
| `src/backend/core/memory/context.py` | `MemoryContext` 与 workspace / 层级解析 |
| `src/backend/orchestration/memory_integration.py` | 检索启动、冻结块组装与注入、保存转调 |
| `src/backend/orchestration/workflow.py` | 主编排：记忆 hook 接线点 |
| `src/backend/api/routes/v1/memories.py` | `/v1/memories` 管理 API |
| `src/backend/core/db/models/memory.py` | `ProfileMemory`、`MemoryOutbox`、`MemorySanitizerRule` ORM |
| `src/backend/edition_ee/db/models/memory.py` | `MemoryAudit` ORM（仅 EE） |
| `src/frontend/src/components/settings/SettingsModal.tsx` | 记忆设置 + 分层记忆弹窗 |
| `src/frontend/src/components/memory/FactsList.tsx` | L2 做法列表组件（可编辑/删除） |
| `docker-compose.yml`（`mem0` profile） | Milvus / etcd / MinIO / Neo4j |
