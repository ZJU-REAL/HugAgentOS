# 知识库

> 最后更新：2026-06-11

HugAgentOS 的知识库提供两种形态，可同时启用、在能力中心统一呈现：

1. **自建知识库**（社区版完整能力）：文档上传 → 父子分块 → 向量化入 Milvus → 稠密 + 稀疏混合检索（RRF 融合）→ 可选重排。包含个人私有知识库与管理员维护的公共知识库（公共知识库管理台属商业版 EE）。
2. **Dify 外接知识库**（商业版 EE）：`KNOWLEDGE_BASE=dify` 时，后端在运行时把 Dify datasets 注入能力中心目录，检索经 Dify Retrieval API 完成。

两种形态最终都以 MCP 工具的形式暴露给智能体：自建走 `retrieve_local_kb`，Dify 走 `retrieve_dataset_content`，均由同一个 MCP server（`mcp_servers/retrieve_dataset_content_mcp/`）提供。

## 整体架构

```
                    ┌──────────────── 能力中心 /v1/catalog ───────────────┐
                    │  api/routes/v1/catalog.py 运行时聚合三类 kb item：    │
                    │  · Dify datasets（is_dify_enabled() 时注入，60s 缓存）│
                    │  · 私有自建知识库（kb_spaces, visibility=private）    │
                    │  · 管理员公共知识库（owner=system_public_kb, public） │
                    └──────────────────────────────────────────────────────┘

  上传入库（自建）                          检索（对话中）
  ─────────────────                        ─────────────────
  POST /v1/catalog/kb/{kb_id}/documents    Agent 调 MCP 工具
    │  validate_kb_file（扩展名+magic）       │
    ▼                                        ├─ retrieve_local_kb（自建）
  对象存储落盘（storage_key）                 │    · embed_text(query)
    │                                        │    · Milvus hybrid_search：
    ▼  BackgroundTask                        │      稠密(IP) + 稀疏(BoW) → RRF(k=60)
  core/content/kb_processing.py              │    · 命中子块 → 回表 kb_chunks 取父块原文
  vectorise_document_background()            │    · 可选 Reranker 重排
    · kb_parser.parse_and_chunk()            │    · user_id 隔离 + public kb 全局可见
    ·（可选）LLM 抽关键词/生成问题            │
    · embed_batch() → Milvus 写入            └─ retrieve_dataset_content（Dify）
    · 父块写 PostgreSQL kb_chunks                 · 调 Dify /datasets/{id}/retrieve
    · 更新 kb_documents.indexing_status           · 多数据集并发→按 score 排序截断
```

## 自建知识库

### 数据模型

ORM 定义在 `src/backend/core/db/models/knowledge.py`：

| 表 | 说明 |
|---|---|
| `kb_spaces` | 知识库空间：owner（`user_id`）、`visibility`（private/public）、`chunk_method`、文档数 / 容量统计 |
| `kb_documents` | 文档：storage_key、checksum、`indexing_status`（processing / completed / failed） |
| `kb_chunks` | **父块**原文（检索命中后返回给 LLM），含标签 `tags` 与关联问题 `questions` |

子块不入关系库——向量化后写 Milvus collection `hugagent_kb_private`（`core/kb/kb_vector.py`），每行带 `user_id` / `kb_id` 字段做归属隔离，`row_type` 区分 chunk 行与 question 行。

### 分块与索引

解析与分块在 `core/kb/kb_parser.py::parse_and_chunk()`，支持五种 `chunk_method`：

| 方法 | 适用 |
|---|---|
| `semantic`（默认） | 通用语义分段 |
| `qa` | 问答对文档 |
| `laws` | 法规条文（按条款切） |
| `recursive` | 递归定长切分 |
| `embedding_semantic` | 基于 embedding 相似度的语义边界检测 |

父子分块参数可在上传时通过 `indexing_config` 调整：`parent_chunk_size`（默认 1024 token）、`child_chunk_size`（128）、`overlap_tokens`（20）、`parent_child_indexing`（默认 true）。还可启用 LLM 增强：`auto_keywords_count`（每父块抽关键词入 tags，参与稀疏检索）与 `auto_questions_count`（每父块生成关联问题，作为独立 question 行入 Milvus，提高问句召回）。后台向量化任务在 `core/content/kb_processing.py::vectorise_document_background()`。

### 检索链路

`mcp_servers/retrieve_dataset_content_mcp/impl.py::retrieve_local_kb`：

1. 解析允许的 `kb_id` 集合（stdio 模式从环境变量、HTTP 模式从 `x-allowed-kb-ids` 等请求头）；
2. `embed_text(query)` 得到查询向量（embedding 配置复用 `MEM0_EMBED_*` 或 DB 中 `embedding` 角色模型）；
3. `core/kb/kb_vector.py::hybrid_search()`：稠密向量（IP 度量）与稀疏向量（词袋 hash，10 万维空间）两路 `AnnSearchRequest`，`RRFRanker(k=60)` 融合；私有库按 `user_id == 当前用户` 过滤，公共库按 `kb_id` 全局放行；
4. 命中子块 / 问题行去重后回 PostgreSQL `kb_chunks` 取**父块原文**返回给 LLM；
5. 用户开启重排时经 Reranker API（`RERANKER_URL/MODEL/API_KEY` 或 DB `reranker` 角色）二次排序。

返回内容带 `[ref:retrieve_local_kb-N]` 引用标记约定，与引用溯源系统（见 [对话模块](./chat.md)）联动。

### API 路由

用户侧路由前缀 `/v1/catalog/kb`（`src/backend/api/routes/v1/kb.py`，请求模型在 `kb_models.py`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/catalog/kb` | 创建知识库空间 |
| PATCH / DELETE | `/v1/catalog/kb/{kb_id}` | 更新 / 删除空间 |
| POST | `/v1/catalog/kb/preview-chunks` | 上传前预览分块效果 |
| POST | `/v1/catalog/kb/polish-description` | AI 生成知识库简介 |
| POST | `/v1/catalog/kb/{kb_id}/documents` | 上传文档（上限 100MB，后台索引） |
| GET | `/v1/catalog/kb/{kb_id}/documents[/{id}]` | 文档列表 / 详情 |
| POST | `/v1/catalog/kb/{kb_id}/documents/{id}/reindex` | 重新索引 |
| GET / PATCH | `/v1/catalog/kb/{kb_id}/chunks[/{chunk_id}]` | 分块列表 / 编辑标签与问题 |

业务逻辑集中在 `core/services/kb_service.py::KBService`。

### 系统托管知识库：我的空间同步

`KBService` 维护一个特殊空间「我的空间同步知识库」（`system_managed=true`，置顶、不可编辑 / 删除 / 手动上传）：用户开启同步开关后，「我的空间」中的文档和图片（含 AI 会话产出）自动同步入库索引，后续新增也持续同步。入口：`POST /v1/artifacts/{artifact_id}/knowledge-base`（手动加入任意空间）与 `KBService.sync_artifact_to_my_space_kb()`（自动同步）。参见 [项目空间与我的空间](./projects-myspace.md)。

### 管理员公共知识库（商业版 EE）

`/v1/admin/kb/*` 管理台路由在 `src/backend/api/routes/v1/admin_kb.py`，挂 `content_admin` 能力位（EE 路由表见 `api/routes/v1/__init__.py`）。公共知识库由合成系统账号 `system_public_kb` 持有（`kb_service.py::SYSTEM_KB_OWNER_ID`），`visibility=public`，对全体用户可见可检索。管理端额外提供原始文件下载、Office 转 PDF 在线预览、分块内容编辑 / 删除等能力。

## Dify 外接知识库（商业版 EE）

客户端封装在 `src/backend/core/kb/dify_kb.py`。启用判定 `is_dify_enabled()` 的优先级：

1. DB 系统配置 `knowledge_base.provider == "dify"`（Config 管理台可改）；
2. 环境变量 `KNOWLEDGE_BASE=dify`；
3. 兜底：`DIFY_URL` + `DIFY_API_KEY` 同时存在。

启用后 `api/routes/v1/catalog.py` 在 `/v1/catalog` 响应中实时注入 Dify datasets 为 `kb` 条目（60 秒进程内缓存），标记 `visibility=public`。检索走 MCP 工具 `retrieve_dataset_content`：不指定 `dataset_id` 时默认并发搜索全部允许的数据集，支持 `hybrid_search` 等 Dify 检索方法参数，结果按 score 排序截断并做 token 上限裁剪。

```bash
KNOWLEDGE_BASE=dify
DIFY_URL=https://your-dify-host/v1     # 兼容别名 DIFY_BASE_URL
DIFY_API_KEY=dataset-...               # 兼容别名 DIFY_AUTH_TOKEN
```

## 文件解析支持

知识库上传校验在 `core/content/file_validation.py::validate_kb_file`（扩展名 + magic bytes 双重校验），允许：`.pdf` `.txt` `.md` `.doc` `.docx` `.xls` `.xlsx` `.csv` `.json` 及图片（`.png` `.jpg` `.jpeg` `.webp` `.gif`）。

通用文件解析器 `core/content/file_parser.py::parse_file()` 覆盖更广（对话附件、我的空间文件共用）：PDF、DOCX、DOC/WPS（经 LibreOffice 转换）、TXT、XLSX/XLS、CSV、PPTX，外加 HTML / Markdown / JSON / YAML / 代码等纯文本格式直接 UTF-8 解码。

## 前端

- 知识库浏览与启停集成在能力中心目录页（`src/frontend/src/components/catalog/`，状态在 `stores/catalogStore.ts`）；
- 创建 / 重建索引弹窗：`src/frontend/src/components/kb/CreateKBModal.tsx`、`ReindexModal.tsx`；
- 管理台公共知识库界面在 `src/frontend/src/components/admin/`（商业版 EE）。

## 相关源码

| 路径 | 职责 |
|---|---|
| `src/backend/core/kb/kb_parser.py` | 文档解析 + 父子分块（5 种 chunk_method） |
| `src/backend/core/kb/kb_vector.py` | Milvus collection、embedding、混合检索、重排 |
| `src/backend/core/kb/dify_kb.py` | Dify datasets 客户端与启用判定 |
| `src/backend/core/content/kb_processing.py` | 后台向量化任务、LLM 关键词 / 问题增强 |
| `src/backend/core/content/file_validation.py` | 上传文件校验（扩展名 + magic bytes） |
| `src/backend/core/content/file_parser.py` | 通用文件解析器 |
| `src/backend/core/services/kb_service.py` | 知识库业务逻辑（含系统托管同步库） |
| `src/backend/api/routes/v1/kb.py` + `kb_models.py` | 用户侧 `/v1/catalog/kb` 路由 |
| `src/backend/api/routes/v1/admin_kb.py` | 管理台公共知识库路由（商业版 EE） |
| `src/backend/api/routes/v1/catalog.py` | 能力目录聚合（Dify 注入 + 私有 / 公共库列表） |
| `src/backend/mcp_servers/retrieve_dataset_content_mcp/` | 检索 MCP server（两个工具） |
| `src/backend/core/db/models/knowledge.py` | `KBSpace` / `KBDocument` / `KBChunk` ORM |
| `src/frontend/src/components/kb/` | 创建 / 重索引弹窗组件 |

相关文档：[MCP 工具](./mcp-tools.md) · [能力目录](./catalog.md) · [对象存储](./storage.md) · [环境变量参考](../deployment/environment-variables.md)
