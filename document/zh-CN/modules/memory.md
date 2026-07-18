# 记忆系统（mem0）

> 最后更新：2026-06-11

HugAgentOS 内置一套**分层持久记忆系统**，基于 [mem0](https://github.com/mem0ai/mem0) 构建：开启后，智能体能跨会话记住用户的身份背景、偏好习惯与历史事实，并在新会话中自动带入这些上下文。记忆按信息稳定性分为三层（L1 档案 / L2 向量事实 / L3 知识图谱），三层均属社区版能力；只有**记忆审计**（合规留痕）属于商业版（商业版 EE）。

整套系统遵循一条核心承诺：**所有记忆 I/O 绝不在 SSE 主链路上同步等待**——检索走带预算超时的后台任务，写入走 SSE 关闭后的有界后置流水线（见 `src/backend/core/memory/__init__.py` 模块文档）。

## 分层模型

| 层 | 名称 | 存储 | 注入时机 | 实现 |
|---|---|---|---|---|
| L1 | Profile 用户档案 | DB（bounded markdown，默认上限 1500 字符） | 会话启动时冻结注入 | `core/memory/profile.py` |
| L2 | Fact 事实记忆 | Milvus 向量库（collection `hugagent_memories`） | 会话启动时按相似度检索 Top-K 注入 | `core/memory/service.py`（mem0 封装） |
| L3 | Graph 图谱记忆 | Neo4j（可选，`MEM0_GRAPH_ENABLED=true`） | 按需检索 | `core/memory/service.py`（mem0 `enable_graph`） |
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
  │          └─ mem0.Memory.search() → Milvus 向量检索（+ Neo4j 图检索，可选）
  │
  ├─► build_frozen_memory_block()          ← 组装"会话冻结块"
  │     · L1 Profile：读 DB，<20ms，必等
  │     · L2 Fact：await 检索 task，预算 600ms（MEMORY_RETRIEVAL_BUDGET_MS）
  │       超时则放弃本轮注入，不阻塞 agent 启动
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
       · 全局 Semaphore 限并发（默认 8）
       · 关键词分类 → 跑 0~4 个 extractor（identity/preference/fact/task）
       · 每个 extractor 单独 30s 超时
       · sanitize 脱敏闸门 → 写 L1/L2/Session → audit 旁路
```

检索与注入的整合层在 `src/backend/orchestration/memory_integration.py`；mem0 配置组装（LLM / Embedder / Milvus / Neo4j / Reranker）在 `src/backend/core/memory/service.py`，模型配置优先取 DB 中 `memory` / `embedding` 角色，缺省回落到环境变量。

## 写入流水线与抽取器

写入只在用户显式开启 `memory_write_enabled` 时发生（第一道门在 `save_memories_background()`，第二道门在 `schedule_post_response_tasks()` 内）。流水线（`core/memory/pipeline.py`）特性：

- **永不 await**：`schedule_post_response_tasks()` 是同步函数，只 `asyncio.create_task()`；
- **有界并发**：全局 `asyncio.Semaphore`（`MEMORY_BG_MAX_CONCURRENCY`，默认 8）；
- **Milvus 熔断器**：连续失败 N 次（默认 3）后短路 60 秒，检索 / 写入路径共用（`milvus_breaker`）；
- **抽取器路由**（`core/memory/extractors/router.py`）：按关键词线索分类本轮对话，命中才跑对应 LLM 抽取器——`identity`（身份）、`preference`（偏好）、`fact`（事实，要求助手回复 >30 字）、`task`（任务）；空集则直接跳过所有 LLM 调用。

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

按 [版本说明](../editions/overview.md)，记忆审计是商业版能力位（`core/licensing/features.py::Feature.MEMORY_AUDIT`）。审计查询接口为 `GET /v1/memories/audit`（支持按 action / layer 过滤）。

## 记忆管理 API

路由文件：`src/backend/api/routes/v1/memories.py`（注册在 CE 路由表中）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/memories` | L2 事实记忆列表；`?project_id=` 按项目 workspace 过滤 |
| GET | `/v1/memories/profile` | L1 用户档案（markdown 全文 + 字符上限） |
| GET | `/v1/memories/graph` | L3 图谱（当前返回 enabled 状态，结构化关系查询待后续版本） |
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
- 「我的分层记忆」弹窗：三个 Tab——档案 L1（markdown 全文）、事实 L2（列表 + 单条删除 + 一键清空，组件 `src/frontend/src/components/memory/FactsList.tsx`）、图谱 L3（未启用时提示需配置 `MEM0_GRAPH_ENABLED` + Neo4j）；
- 项目维度的记忆查看：`src/frontend/src/components/projects/ProjectMemoriesModal.tsx`；
- API 封装：`src/frontend/src/api.ts`（`getMemories` / `getMemoryProfile` / `getMemoryGraph` / `getMemorySettings` 等）。

## 基础设施

L2/L3 依赖的向量库与图数据库通过 Docker Compose `mem0` profile 一键启动（不启用时主应用零开销短路）：

```bash
docker-compose --profile mem0 up -d
```

| 服务 | 镜像 | 作用 |
|---|---|---|
| milvus | `milvusdb/milvus:v2.4.0`（standalone） | L2 向量存储 |
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
MEMORY_EXTRACT_TIMEOUT_S=30       # 单 extractor 超时
MEMORY_PROFILE_MAX_CHARS=1500     # L1 档案字符上限
MEMORY_FACT_DEFAULT_TTL_DAYS=180  # L2 事实默认 TTL
MEMORY_FROZEN_TOPK=5              # 冻结块 Fact Top-K
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
| `src/backend/core/memory/service.py` | mem0 配置组装与异步封装（Milvus / Neo4j / Reranker） |
| `src/backend/core/memory/profile.py` | L1 档案：get / patch / compact / delete |
| `src/backend/core/memory/pipeline.py` | 后置写入流水线、信号量、Milvus 熔断器 |
| `src/backend/core/memory/extractors/` | identity / preference / fact / task 抽取器 + 关键词路由 |
| `src/backend/core/memory/sanitizer.py` | 脱敏闸门（硬编码规则 + DB 动态规则） |
| `src/backend/core/memory/audit.py` | 审计旁路（商业版 EE） |
| `src/backend/core/memory/context.py` | `MemoryContext` 与 workspace / 层级解析 |
| `src/backend/orchestration/memory_integration.py` | 检索启动、冻结块组装与注入、保存转调 |
| `src/backend/orchestration/workflow.py` | 主编排：记忆 hook 接线点 |
| `src/backend/api/routes/v1/memories.py` | `/v1/memories` 管理 API |
| `src/backend/core/db/models/memory.py` | `MemoryAudit` / `MemorySanitizerRule` ORM |
| `src/frontend/src/components/settings/SettingsModal.tsx` | 记忆设置 + 分层记忆弹窗 |
| `src/frontend/src/components/memory/FactsList.tsx` | L2 事实列表组件 |
| `docker-compose.yml`（`mem0` profile） | Milvus / etcd / MinIO / Neo4j |
