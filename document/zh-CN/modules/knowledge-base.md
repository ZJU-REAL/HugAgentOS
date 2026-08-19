# 知识库

> 最后更新：2026-07-19

HugAgentOS 的知识库提供两种形态，可同时启用、在能力中心统一呈现：

1. **自建知识库**：文档上传 → 父子分块 → 向量化入 Milvus → 稠密 + 稀疏混合检索（RRF 融合）→ 可选重排。社区版 CE 仅提供当前用户拥有的私有知识库；管理员维护的公共知识库属于商业版 EE。
2. **Dify 外接知识库**（商业版 EE）：`KNOWLEDGE_BASE=dify` 时，后端在运行时把 Dify datasets 注入能力中心目录，检索经 Dify Retrieval API 完成。

两种形态最终都以 MCP 工具的形式暴露给智能体：自建走 `retrieve_local_kb`，Dify 走 `retrieve_dataset_content`，均由同一个 MCP server（`mcp_servers/retrieve_dataset_content_mcp/`）提供。

社区版 CE 的前端只显示「私有知识库」，`/v1/catalog` 也只返回当前用户的私有库。后端会拒绝 `visibility=public` 的创建请求，并且不提供 Dify / 共享知识库服务配置，避免仅靠前端隐藏造成能力越界。

## 整体架构

```
                    ┌──────────────── 能力中心 /v1/catalog ───────────────┐
                    │  api/routes/v1/catalog.py 运行时聚合三类 kb item：    │
                    │  · 私有自建知识库（CE + EE）                          │
                    │  · Dify datasets（仅 EE，启用时注入，60s 缓存）       │
                    │  · 管理员公共知识库（仅 EE，system_public_kb）        │
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

### 索引模式：RAG / Wiki

建库时可勾选两种索引方式，存在 `kb_spaces.metadata.index_modes`：

| 勾选 | `index_modes` | 分块 | 向量 / Milvus | Wiki 生成 |
|---|---|---|---|---|
| 仅 RAG 检索（默认） | `["rag"]` | ✅ | ✅ | ❌ |
| 仅 Wiki 图谱 | `["wiki"]` | ✅ | ❌ | ✅ |
| 两者都选 → **LLM-Wiki 知识库** | `["rag","wiki"]` | ✅ | ✅ | ✅ |

> 三种模式都会分块写 `kb_chunks`。Wiki 的引文标注按分块走、回溯原文按分块直取，
> 脱离分块无从谈起——「仅 Wiki」省掉的是向量化，不是分块。

历史知识库的 metadata 里没有这个键，读出来即缺省的仅 RAG，与它们已建好的索引一致。
详见 [知识库 Wiki](./knowledge-base-wiki.md)。

### 数据模型

ORM 定义在 `src/backend/core/db/models/knowledge.py`：

| 表 | 说明 |
|---|---|
| `kb_spaces` | 知识库空间：owner（`user_id`）、`visibility`（private/public）、`chunk_method`、文档数 / 容量统计 |
| `kb_documents` | 文档：storage_key、checksum、`indexing_status`（processing / completed / failed） |
| `kb_chunks` | **父块**原文（检索命中后返回给 LLM），含标签 `tags` 与关联问题 `questions` |
| `kb_assets` | 文档里的**媒体资产**（版面解析切出的图片、单独上传的图片）：字节在对象存储，本表存归属分块、原文位置 `locator`、图注/OCR `text_content`、模型生成的描述 `caption` |

Wiki 相关的三张表（`kb_wiki_pages` / `kb_wiki_folders` / `kb_wiki_jobs`）定义在 `core/db/models/kb_wiki.py`，见 [知识库 Wiki](./knowledge-base-wiki.md)。

子块不入关系库——向量化后写 Milvus collection `hugagent_kb_private`（`core/kb/kb_vector.py`），每行带 `user_id` / `kb_id` 字段做归属隔离，`row_type` 区分 chunk 行、question 行与 image 行。

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

### 多模态：图片检索

文档里的图不再被丢弃。索引时走 `core/kb/kb_assets.py`：

1. **抽图**——PDF 经外部解析服务的版面切分拿回图片本体、图注、页码与 bbox；单独上传的
   图片自身即一个资产（此前会被当作纯文本硬解成乱码入库，现已修复）。字节存对象存储，
   正文里原本指向解析服务临时路径的死链被改写成真实资产 URL。
2. **图像理解**——复用[视觉桥](./model-providers.md#视觉桥让纯文本模型能读图)（`core/vision`），和对话里「纯文本主模型读图」
   走的是同一条通路、同一个 `vision` 模型角色，因此白得三样东西：按图片内容哈希的**证据缓存**
   （重新索引同一篇文档不用再付一次钱）、三协议与结构化输出降级、以及「该角色只能指派多模态模型」
   的硬约束。产出映射：`summary` → `caption`，`ocr.full_text` 并入 `text_content`（与原图注共存），
   实体 / 关系 / 不确定项留档进 `metadata.vision` 供本体层与人工复核使用。
   **未指派 `vision` 角色（且主模型不识图）是正常状态**：图片仍以图注入索引，之后配好再回填即可，
   无需重新解析文档。
3. **入索引**——每个资产写一条 `row_type="image"` 的检索行，`parent_chunk_id` 指向包含它
   的父块，因此去重、回表取父块原文、重排、按文档删除等既有链路全部无改动复用。
4. **回传**——检索结果里该片段会带 `images: [{asset_id, url, caption}]`，供模型据描述作答、
   供前端渲染缩略图。图片本体经 `GET /v1/catalog/kb/assets/{asset_id}` 读取，可见性完全
   跟随所属知识库空间。

开关与调优：`indexing_config.multimodal_indexing`（单库）优先于全局 `KB_MULTIMODAL_INDEXING`。
单张图的体积上限、调用超时、证据缓存有效期都归视觉桥管（`VISION_*`），知识库侧只管**批量节流**：
`KB_ASSET_CAPTION_BATCH` 限制一次提交给视觉桥的张数——它的并发信号量是进程级的，一篇上百张图的
文档若一次性压过去，会把对话里的看图请求全排到后面。详见
[环境变量参考](../deployment/environment-variables.md)。

索引跑在 `BackgroundTasks` 的工作线程里（无运行中事件循环），而视觉桥是异步的：这里用
`anyio.from_thread` 把协程交回**宿主循环**执行（Starlette 的 `run_in_threadpool` 即
`anyio.to_thread.run_sync`），从而真正用上证据缓存与信号量。只有拿不到宿主循环时（CLI / 脚本）
才起临时循环并关闭缓存——Redis 连接池是模块级全局、绑定首次使用它的循环，让临时循环抢先创建它
会把整个进程的 Redis 绑死在已销毁的循环上。

音视频沿用同一条链路：`kb_assets.kind` 已放宽到 `audio` / `video`，接入时只需把
ASR 转写写进 `text_content`，其后的索引与检索完全复用。

### 检索链路

`mcp_servers/retrieve_dataset_content_mcp/impl.py::retrieve_local_kb`：

1. 解析允许的 `kb_id` 集合（stdio 模式从环境变量、HTTP 模式从 `x-allowed-kb-ids` 等请求头）；
2. `embed_text(query)` 得到查询向量（embedding 配置复用 `MEM0_EMBED_*` 或 DB 中 `embedding` 角色模型）；
3. `core/kb/kb_vector.py::hybrid_search()`：稠密向量（IP 度量）与稀疏向量（词袋 hash，10 万维空间）两路 `AnnSearchRequest`，`RRFRanker(k=60)` 融合；私有库按 `user_id == 当前用户` 过滤，EE 公共库按授权后的 `kb_id` 放行；
4. 命中子块 / 问题行 / 图片行去重后回 PostgreSQL `kb_chunks` 取**父块原文**返回给 LLM，并附上该片段的图片引用；
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
| POST | `/v1/catalog/kb/{kb_id}/wiki/rebuild` | 为已有文档补建 Wiki（需编辑权限） |
| GET | `/v1/catalog/kb/{kb_id}/wiki/*` | Wiki 读取面，见 [知识库 Wiki](./knowledge-base-wiki.md) |
| GET / PATCH | `/v1/catalog/kb/{kb_id}/chunks[/{chunk_id}]` | 分块列表 / 编辑标签与问题 |
| GET | `/v1/catalog/kb/assets/{asset_id}` | 读取媒体资产原始字节（可见性跟随所属空间） |

业务逻辑集中在 `core/services/kb_service.py::KBService`。

### 系统托管知识库：我的空间同步

`KBService` 维护一个特殊空间「我的空间同步知识库」（`system_managed=true`，置顶、不可编辑 / 删除 / 手动上传）：用户开启同步开关后，「我的空间」中的文档和图片（含 AI 会话产出）自动同步入库索引，后续新增也持续同步。入口：`POST /v1/artifacts/{artifact_id}/knowledge-base`（手动加入任意空间）与 `KBService.sync_artifact_to_my_space_kb()`（自动同步）。参见 [项目空间与我的空间](./projects-myspace.md)。

### 管理员公共知识库（商业版 EE）

`/v1/admin/kb/*` 管理台路由在 `src/backend/api/routes/v1/admin_kb.py`，挂 `content_admin` 能力位（EE 路由表见 `api/routes/v1/__init__.py`）。公共知识库由合成系统账号 `system_public_kb` 持有（`kb_service.py::SYSTEM_KB_OWNER_ID`），`visibility=public`，对全体用户可见可检索。管理端额外提供原始文件下载、Office 转 PDF 在线预览、分块内容编辑 / 删除等能力。

## Dify 外接知识库（商业版 EE）

商业版客户端位于 `src/backend/edition_ee/kb/dify.py`，共享路由通过 `core/kb/external_provider.py` 接缝调用。CE 派生树把该接缝替换为禁用实现，且不包含 Dify 客户端。启用判定 `is_dify_enabled()` 的优先级：

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

- 知识库浏览与启停集成在能力中心目录页（`src/frontend/src/components/catalog/`，状态在 `stores/catalogStore.ts`）；CE 只显示私有知识库，EE 显示公共 / 私有两个模块；
- 创建 / 重建索引弹窗：`src/frontend/src/components/kb/CreateKBModal.tsx`、`ReindexModal.tsx`；
- 媒体资产缩略图 `components/kb/KBAssetThumbs.tsx`：分块查看页与对话里的检索结果卡 / 引用卡共用，
  点击放大。检索结果优先用命中片段带回的 `images`（含图注），历史命中回退到从分块正文里抠资产链接；
- 管理台公共知识库界面在 `src/frontend/src/components/admin/`（商业版 EE）。

## 相关源码

| 路径 | 职责 |
|---|---|
| `src/backend/core/kb/kb_parser.py` | 文档解析 + 父子分块（5 种 chunk_method） |
| `src/backend/core/kb/kb_vector.py` | Milvus collection、embedding、混合检索、重排 |
| `src/backend/core/kb/kb_assets.py` | 媒体资产：落盘、图像理解（调用视觉桥）、检索行、结果附图 |
| `src/backend/edition_ee/kb/dify.py` | Dify datasets 客户端与启用判定（仅 EE） |
| `src/backend/core/kb/external_provider.py` | 版本中立的外部知识库接缝；CE overlay 将其禁用 |
| `src/backend/core/content/kb_processing.py` | 后台向量化任务、LLM 关键词 / 问题增强 |
| `src/backend/core/content/file_validation.py` | 上传文件校验（扩展名 + magic bytes） |
| `src/backend/core/content/file_parser.py` | 通用文件解析器 |
| `src/backend/core/services/kb_service.py` | 知识库业务逻辑（含系统托管同步库） |
| `src/backend/api/routes/v1/kb.py` + `kb_models.py` | 用户侧 `/v1/catalog/kb` 路由 |
| `src/backend/api/routes/v1/admin_kb.py` | 管理台公共知识库路由（商业版 EE） |
| `src/backend/api/routes/v1/catalog.py` | 能力目录聚合（CE 仅私有库；EE 追加 Dify / 公共库） |
| `src/backend/mcp_servers/retrieve_dataset_content_mcp/` | 检索 MCP server（两个工具） |
| `src/backend/core/db/models/knowledge.py` | `KBSpace` / `KBDocument` / `KBChunk` / `KBAsset` ORM |
| `src/frontend/src/components/kb/` | 创建 / 重索引弹窗组件 |

相关文档：[MCP 工具](./mcp-tools.md) · [能力目录](./catalog.md) · [对象存储](./storage.md) · [环境变量参考](../deployment/environment-variables.md)
