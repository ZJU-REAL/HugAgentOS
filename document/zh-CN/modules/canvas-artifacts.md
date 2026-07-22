# 数据画布与产物

> 最后更新：2026-06-11

对话产生的文件（AI 生成的报告、图表、表格、代码运行结果）在 HugAgentOS 中统一抽象为 **Artifact（产物）**：后端有持久化存储与权限受控的下载通道，前端有两类查看 / 编辑面板（数据画布、我的空间），还可以一键生成对外分享链接。本篇覆盖整条链路：产物怎么产生、存在哪、在哪看、怎么改、怎么分享。

个人画布编辑、产物中心、会话分享均属**社区版（CE）**；画布**多人实时协同**属商业版 EE（License 能力位 `canvas_collab`），云存储（S3 / OSS）属商业版 EE（`cloud_storage`）。

## 数据画布（Canvas）

数据画布是聊天界面右侧的滑出面板（`src/frontend/src/components/canvas/CanvasPanel.tsx`），点击聊天附件卡片（`components/chat/ArtifactCardList.tsx`）或「我的空间」中的文件（`components/myspace/MySpacePanel.tsx`）即可打开。面板按文件类型选择渲染器：

| 文件类型 | 渲染方式 | 可编辑 |
|---|---|---|
| `.xlsx` / `.xls` | **Univer 在线表格**（见下） | ✅ 单元格 / 公式编辑，可保存回写 |
| `.docx` / `.doc` | docx 预览渲染 | 只读 |
| `.pdf` | 内嵌 PDF 预览 | 只读 |
| `.pptx` / `.ppt` | 转换预览 | 只读 |
| 图片 | 直接展示 | 只读 |
| 文本 / 代码（txt、md、csv、json、py …） | 文本渲染 | 只读 |
| `.html` | HTML 预览 | 只读 |

面板状态由 `stores/canvasStore.ts` 管理（`openCanvas` / `closeCanvas` / `updateArtifact`，`openSeq` 用于区分「打开新文件」与「同文件保存后刷新」）。

### Univer 表格集成

`components/canvas/UniverSpreadsheet.tsx` 负责 xlsx 的在线编辑：

1. 用 SheetJS（`xlsx` 包）解析文件，转换为 Univer 的 `IWorkbookData` 格式（含多 Sheet、数字 / 布尔 / 公式单元格、合并区域，公式引用经 `utils/xlsxRange.ts::recomputeSheetRefs` 重算）。
2. 运行时动态 `import('@univerjs/presets')` + `@univerjs/preset-sheets-core`（中文语言包）渲染电子表格——**实际只加载免费的 core 预设**。
3. 编辑后通过 `exportXlsx()` 导出为新的 xlsx File，由 `CanvasPanel` 调 `api.ts::overwriteFile` 回写到同一个 `file_id`，dirty 状态驱动「保存」按钮。

> 多人实时协同编辑为商业版 EE 能力（`Feature.CANVAS_COLLAB`，`edition_ee/licensing/features.py`）。注意 `src/frontend/package.json` 目前仍声明了 `@univerjs/preset-sheets-advanced` 依赖（Univer 商业 License 预设）——运行时代码并未导入它；按开源方案，CE 派生树不应携带该依赖。

## Artifact 中心（我的空间）

### 产物的产生与落库

对话流式过程中，工具产出的文件先进入会话**工作区**（`core/llm/workspace`），只有被「钉住」（pinned）的文件才会在回合结束时由 `core/services/artifact_service.py::persist_artifacts` 写入 `artifacts` 表——这道**严格工作区闸门**保证临时中间文件不会污染我的空间。

物理存储由 `core/artifacts/store.py` 管理，按 `STORAGE_TYPE` 双模式：

- `local`（默认，社区版）：文件写在 `${STORAGE_PATH:-result}/artifacts/`，维护本地 JSON 索引（`index.json`），FileResponse 直出。
- `oss`（商业版 EE，`cloud_storage`）：上传到阿里云 OSS，本地索引同时备份到 OSS，容器重启不丢。S3 后端见 `core/storage/`。

下载统一走 `GET /files/{file_id}`（`api/routes/files.py`），按 `core/auth/permissions_iface.py::resolve_artifact_access` 做 owner ∪ team 权限判定，Office 文件支持转 PDF 在线预览。

### REST 接口（`/v1/artifacts`，`api/routes/v1/artifacts.py`）

| 接口 | 说明 |
|---|---|
| `GET ""` | 文件 / 图片列表：按 `type`（document/image）、`source_kind`（user_upload/ai_generated）、关键词、`scope`（personal/all 含团队）、个人文件夹 `folder_id` 过滤，分页 |
| `GET /favorites` | 收藏的会话列表（`ChatSession.favorite` 标记，含最后一条消息预览） |
| `POST /{artifact_id}/knowledge-base` | 把产物一键加入知识库（后台向量化，见 [知识库](knowledge-base.md)） |
| `DELETE /{artifact_id}` | 软删除 |

前端「我的空间」面板在 `src/frontend/src/components/myspace/`（`MySpacePanel.tsx` 文件列表 + `FavoriteList.tsx` 收藏），状态在 `stores/mySpaceStore.ts`；个人文件夹由 `api/routes/v1/myspace_folders.py` 管理，详见 [项目与我的空间](projects-myspace.md)。

## 会话分享

`api/routes/v1/chat_shares.py` 提供会话只读分享链接：

| 接口 | 说明 |
|---|---|
| `POST /v1/chat-shares` | 生成分享：传入会话消息快照（含计划卡片数据），有效期 `3d` / `15d` / `3m` / `permanent` |
| `GET /v1/chat-shares/{share_id}` | 公开读取分享内容（无需登录） |
| `GET /v1/chat-shares` | 我的分享记录 |
| `POST /{share_id}/revoke` / `restore` | 终止 / 恢复访问 |
| `DELETE /{share_id}` | 删除记录 |

分享内容是消息**快照**，存储在 Redis（`chat_share:*` 三组 Key），Redis 不可用时退化为进程内字典。访问端是独立的轻量页面 `src/frontend/src/SharePreviewApp.tsx`——`main.tsx` 检测到 URL 带 `?share=<share_id>` 参数即渲染分享视图而非主应用。分享记录管理页在 `src/frontend/src/components/share/ShareRecordsPage.tsx`。

## 图表生成如何落到产物

MCP 工具 `generate_chart_tool`（`src/backend/mcp_servers/generate_chart_tool_mcp/`）用 matplotlib 按数据 + 指令绘图（折线 / 柱状 / 饼图等），生成的 PNG 通过 `core/artifacts/store.py::save_artifact_bytes` 直接落为产物，返回：

```json
{"ok": true, "file_id": "<artifact id>", "url": "/files/<file_id>",
 "name": "chart_xxx.png", "mime_type": "image/png"}
```

图表立即出现在聊天附件区，可在画布中预览、在我的空间收纳。工具描述中强制「先取数后绘图」，禁止凭空编数据。若要把图表插入沙箱里正在生成的 Word/PPT，需先用 `sandbox_put_artifact` 把 `file_id` 拷进沙箱再由 CLI 引用沙箱路径——artifact 存储与沙箱文件系统是两个世界。

## 报告导出如何落到产物

MCP 工具组 `report_export_mcp`（`src/backend/mcp_servers/report_export_mcp/`）做轻量 Markdown → Office 导出，同样经 `save_artifact_bytes` 落库：

| 工具 | 说明 |
|---|---|
| `export_report_to_docx` | Markdown 报告 → .docx（公文字体：标题方正小标宋、正文方正仿宋；基于 `reference.docx` 模板）。**已标记 DEPRECATED**——复杂排版（自定义样式、页眉页脚、目录、插图、模板套打）请改用 word 编辑技能 `word-cli create` |
| `export_table_to_excel` | 解析 Markdown 表格 → .xlsx 下载 |

返回的 `file_id` / `url` 与图表一致，自动出现在附件区；xlsx 还能直接在数据画布里继续编辑。

## 端到端示例

> 用户：「查一下近五年新能源汽车产量，画成柱状图，再把分析导出成 Word。」

1. 智能体先用搜索 / 数据工具取数；
2. 调 `generate_chart_tool` 生成柱状图 → artifact A（PNG，附件区可见，画布可预览）；
3. 撰写分析文本后调 `export_report_to_docx` → artifact B（docx）；
4. 两个产物随 `meta` 事件钉入工作区、由 `persist_artifacts` 落库，出现在「我的空间」；
5. 用户点击 docx 在画布预览，满意后 `POST /v1/chat-shares` 生成 15 天有效的分享链接发给同事。

## 相关源码

| 主题 | 路径 |
|---|---|
| 画布面板 / Univer 表格 | `src/frontend/src/components/canvas/CanvasPanel.tsx`、`UniverSpreadsheet.tsx`、`src/frontend/src/stores/canvasStore.ts` |
| Artifact REST API | `src/backend/api/routes/v1/artifacts.py` |
| Artifact 存储（local/OSS） | `src/backend/core/artifacts/store.py` |
| 产物落库（工作区闸门） | `src/backend/core/services/artifact_service.py` |
| 文件下载 / 预览 | `src/backend/api/routes/files.py` |
| 会话分享 API | `src/backend/api/routes/v1/chat_shares.py` |
| 分享前端 | `src/frontend/src/SharePreviewApp.tsx`、`src/frontend/src/components/share/ShareRecordsPage.tsx` |
| 图表生成 MCP | `src/backend/mcp_servers/generate_chart_tool_mcp/server.py`、`chart.py` |
| 报告导出 MCP | `src/backend/mcp_servers/report_export_mcp/server.py`、`impl.py` |
| 我的空间前端 | `src/frontend/src/components/myspace/`、`src/frontend/src/stores/mySpaceStore.ts` |

延伸阅读：[沙箱](sandbox.md) · [项目与我的空间](projects-myspace.md) · [存储](storage.md) · [MCP 工具](mcp-tools.md)
