# MCP 工具系统

> 最后更新：2026-07-02

HugAgentOS 的工具生态构建在 [MCP（Model Context Protocol）](https://modelcontextprotocol.io) 之上：每一类外部能力（联网搜索、网页抓取、数据库查询、图表生成……）都是一个独立的 MCP Server，统一运行在专用的 `mcp` 容器中，后端通过 streamable-http 协议连接调用。这种设计带来三点好处：

- **插拔粒度在 Server 级**——启用/禁用一个能力只需改一行 catalog 配置或在管理台开关，无需改代码；
- **故障隔离**——单个工具崩溃由 launcher 自动重启，不影响后端主进程；
- **生态开放**——管理员可接入任意第三方 MCP Server（stdio / HTTP / SSE），用户也可自助添加私有远程 MCP。

## 整体架构

```
                       ┌──────────────────────── mcp 容器 (docker/Dockerfile.mcp) ───┐
                       │  mcp_servers._launcher（每个 server 一个子进程）       │
┌─────────┐  HTTP  ┌───┴────┐   :9100  retrieve_dataset_content（知识库检索）   │
│ backend │───────▶│streama-│   :9101  query_database（数仓查询，EE）           │
│ (FastAPI│        │ble-http│   :9102  internet_search（联网搜索）              │
│  agent) │        │        │   :9103  ai_chain_information_mcp（产业链，EE）   │
└─────────┘        │        │   :9104  generate_chart_tool（图表生成）          │
     │             │        │   :9105  report_export_mcp（表格导出）            │
 MCPConnectionPool │        │   :9106  web_fetch（网页抓取）                    │
 (core/llm/        │        │   :9107  batch_runner（批量计划）                 │
  mcp_pool.py)     └───┬────┘   :9108  automation_task（定时任务管理）        │
                       │        9109–9111 保留（原 excel/ppt/pdf MCP）        │
                       │        :9112  skill_manager（技能管理）              │
                       └──────────────────────────────────────────────────────┘
```

端口分配的唯一真源是 `src/backend/mcp_servers/_ports.py`：`core/config/mcp_config.py`（后端拼 `http://mcp:NNNN/mcp/` URL）和 `mcp_servers/_launcher.py`（容器内绑定端口）都从这里读取。

> 历史说明：办公文档编辑（word / excel / ppt / pdf）的 MCP Server 已整体迁出 `mcp` 容器，改为以 [Agent 技能](agent-skills.md)（word-editing / excel-editing / ppt-design / pdf-editing）的形态在沙箱容器内执行，各技能自带引擎。因此 `docker/Dockerfile.mcp` 不再安装 LibreOffice / .NET / Node / Chromium；9108 已复用于自动化任务管理，9109–9111 仍保留。

## 内置 MCP Server 一览

| Server（目录） | 端口 | 工具 | 版本 |
|---|---|---|---|
| `retrieve_dataset_content_mcp` | 9100 | `retrieve_dataset_content` / `list_datasets` / `retrieve_local_kb` | 社区版 CE |
| `query_database_mcp` | 9101 | `query_database` | **商业版 EE** |
| `internet_search_mcp` | 9102 | `internet_search` | 社区版 CE |
| `ai_chain_information_mcp` | 9103 | 13 个产业链/企业画像工具（见下） | **商业版 EE** |
| `generate_chart_tool_mcp` | 9104 | `generate_chart_tool` | 社区版 CE |
| `report_export_mcp` | 9105 | `export_table_to_excel` | 社区版 CE |
| `web_fetch_mcp` | 9106 | `web_fetch` | 社区版 CE |
| `batch_runner_mcp` | 9107 | `batch_plan` | 社区版 CE |
| `automation_task_mcp` | 9108 | `create_scheduled_task` / `list_scheduled_tasks` / `update_scheduled_task` 等 | 社区版 CE |
| `skill_manager_mcp` | 9112 | `search_marketplace` / `install_from_marketplace` / `register_skill` / `list_my_skills` / `submit_to_marketplace` / `delete_skill` | 社区版 CE |

> 版本边界以 [开源与商业化产品方案](../editions/overview.md) 为准：依赖内网行业数据源（产业知识中心、数据仓库）的两个行业 Server 属商业版（社区版派生树通过 `ce/manifest.yaml` 整目录剔除并从 `catalog.json` 摘除种子），其余 8 个通用工具全部进社区版。

### retrieve_dataset_content — 知识库检索（CE）

知识库 RAG 的检索入口，一个 Server 暴露三个工具：

- **`retrieve_dataset_content(query, dataset_id, top_k, score_threshold, search_method, reranking_enable, weights)`**：对接 Dify 外部知识库的语义/混合检索；
- **`list_datasets()`**：列出当前用户可用的全部知识库（公有 + 私有），含名称、简介与文档列表，供模型先探查再检索；
- **`retrieve_local_kb(kb_id, query, top_k)`**：检索平台自建私有知识库。

它是唯一的 **per-request** Server：每次对话请求按当前用户把允许访问的知识库 ID、用户 ID、重排序开关通过 **HTTP 请求头**（`X-Allowed-Dataset-Ids` / `X-Allowed-Kb-Ids` / `X-Current-User-Id` / `X-Reranker-Enabled`）注入（见 `core/llm/agent_factory.py::_apply_runtime_kb_constraints`），Server 端用 `ctx.request_context` 读头实现多用户隔离。详见[知识库模块](knowledge-base.md)。

### query_database — 数据仓库查询（商业版 EE）

`query_database(question, 工号)`：把用户的完整自然语言问题整体传给内网数据仓库服务，由其内部完成问题分解、多表联查与 NL2SQL，返回可核对的精确指标数值（如规上工业增加值、增速、利润总额）。工具描述中将其定为"精确数值类问题的最高优先级数据源"。**完全依赖内网数仓，无法脱离行业数据运行，属商业版。**

### internet_search — 联网搜索（CE）

`internet_search(query, max_results, topic, search_depth, include_raw_content, cn_only)`：基于 Tavily 等搜索引擎的互联网检索（`TAVILY_API_KEY` / `INTERNET_SEARCH_ENGINE` 配置），支持 general / news / finance 主题与多档检索深度。在工具调用决策上被定位为**兜底工具**——内部知识库、数仓、行业工具都无结果时才使用。

### ai_chain_information_mcp — 产业知识中心查询（商业版 EE）

一个分组式 Server，把产业链分析与企业画像 13 个工具收编在同一 MCP 下（插拔粒度保持在 Server 级），实现拆分为 `impl_chain / impl_news / impl_latest / impl_company / impl_entity / impl_rank` 多个模块：

| 工具 | 用途 |
|---|---|
| `get_chain_information(chain_id)` | 产业链全景分析报告与核心指标 |
| `get_industry_news(keyword, news_type, chain, region)` | 产业动态资讯 |
| `get_latest_ai_news()` | AI 领域热点聚合 |
| `search_company(keyword, top_num)` | 企业模糊搜索 |
| `get_company_base_info(company_id)` | 企业基础画像 |
| `get_company_business_analysis(company_id)` | 企业经营分析 |
| `get_company_tech_insight(company_id)` | 企业技术洞察 |
| `get_company_funding(company_id)` | 企业融资信息 |
| `get_industry_hot_companies(...)` | 行业热门企业榜 |
| `get_industry_hot_products(...)` | 行业热门产品榜 |
| `get_company_hot_events(...)` | 企业热点事件 |
| `get_product_detail(...)` | 产品详情 |
| `get_company_risk_warning(company_id)` | 企业风险预警 |

**依赖内网"产业知识中心"数据源，属商业版。**

### generate_chart_tool — 数据可视化（CE）

`generate_chart_tool(data, query)`：接收 JSON 数据与绘图指令，用 matplotlib 渲染折线图/柱状图/饼图等（mcp 容器内置文泉驿 + 方正中文字体保证 CJK 渲染），图片保存为平台 artifact 并返回 `file_id` / 下载 URL。工具描述强制要求"先用数据查询工具拿到真实数据再绘图"，并给出了与沙箱协作的标准链路（`sandbox_put_artifact` 把图表拷入沙箱后再插入 Word/PPT）。

### report_export_mcp — 轻量表格导出（CE）

`export_table_to_excel(markdown, title, filename)`：把对话中已生成的 Markdown 表格一键转换为带基础样式的 .xlsx 下载（每个表格一个 sheet）。需要公式、多 sheet 模型、编辑既有文件等完整能力时走 excel-editing 技能。

> 该 Server 原有的 `export_report_to_docx`（Markdown → 公文样式 Word）**MCP 入口已下线**，由 word-editing 技能的 `word-cli create --markdown` 取代；函数体保留仅供 selftest 回归（见 `report_export_mcp/server.py` 头部注释）。

### web_fetch — 网页抓取（CE）

`web_fetch(url, extractMode, maxChars)`：抓取指定 URL 并提取正文，支持 `text` / `markdown` / `html` 三种提取模式。典型搭配是"先 `internet_search` 拿 URL，再 `web_fetch` 取正文"；多个搜索类市场技能也通过它调用专门搜索引擎 URL。

### batch_runner — 批量执行调度器（CE）

`batch_plan(instruction, file_ids, text_items, chat_id)`：识别"对一组对象逐个做同一件事"的批量意图（枚举对象 / 上传 Excel 行 / 多份文档），生成带 prompt 模板与占位符的**执行计划**并立即暂停回合——前端弹出确认对话框，用户审阅/修改模板后由后端逐条执行并实时推送结果。详见[批量执行 / 自动化模块](automation.md)。

### automation_task — 定时任务管理（CE）

用于让智能体在对话中直接维护当前用户的自动化任务：`create_scheduled_task` 创建定时任务，`list_scheduled_tasks` / `get_scheduled_task` 查看任务，`update_scheduled_task` 修改 Cron、提示词和状态，`pause_scheduled_task` / `resume_scheduled_task` / `delete_scheduled_task` 执行生命周期操作。身份从 `X-Current-User-Id` 请求头注入，只能操作当前用户自己的任务。

### skill_manager — 技能管理（CE）

服务于能力中心和技能管理类插件：`search_marketplace` 搜索技能市场，`install_from_marketplace` 安装市场技能，`register_skill` 从上传包注册个人技能，`list_my_skills` 查看当前用户技能，`submit_to_marketplace` 提交上架申请，`delete_skill` 删除个人技能。服务层复用技能权限位与 owner 隔离，CE/EE 都按当前用户边界执行。

## 统一的 Server 工程结构

每个内置 Server 遵循同一套目录约定：

```
mcp_servers/<name>_mcp/
├── server.py        # FastMCP 实例 + @mcp.tool() 薄壳（参数容错、stdout 重定向到 stderr）
├── impl.py          # 业务实现（server.py 内延迟 import，保持启动轻量）
├── _selftest.py     # 自检脚本：不出网验证模块可导入、签名正确
└── README.md        # 运行/调试说明
```

公共层（`mcp_servers/` 根目录）：

| 文件 | 职责 |
|---|---|
| `_serve.py` | 所有 Server `main()` 的统一入口：`run(mcp, default_port)` 按 `--transport` 选 stdio（本地调试默认）或 streamable-http（容器内），HTTP 模式下绑 `0.0.0.0` 并关闭 DNS-rebinding 防护（私有 Docker 网络） |
| `_launcher.py` | `mcp` 容器的 CMD：为每个 Server 起一个 streamable-http 子进程，stdout/stderr 加 `[server]` 前缀，崩溃指数退避重启，60 秒内崩溃超 5 次则整容器退出交给 Docker 重启 |
| `_ports.py` | server_id → 端口映射的唯一真源，含 `package_name()` 包名换算 |
| `_common.py` | 共享工具函数 |

两条铁律：**stdout 保留给 MCP 协议**（业务日志一律走 stderr，server.py 里用 `contextlib.redirect_stdout` 兜底）；**对 LLM 生成的畸形参数保持容错**（如 dict 误塞进字符串参数时自动拆包）。

## 后端客户端：连接池与裸名还原

后端基于 AgentScope 2.0 的 `MCPClient` 连接 MCP Server，核心在两个文件：

- **`core/llm/mcp_pool.py` — `MCPConnectionPool`**（进程级单例）：启动时 `warmup_mcp_tools()` 从 DB 读取全部启用的 Server 配置并预连接。注意 2.0 语义下的池化策略：
  - **stdio 且 `is_stable=true`** 的 Server 跨请求保持连接（省掉 1–7 秒子进程冷启动）；
  - **HTTP Server 一律不池化**——2.0 的 stateful HTTP 客户端与 asyncio task 绑定，跨请求复用会触发 cancel-scope 崩溃，因此每请求用 `is_stateful=False` 新建连接；
  - per-request Server（知识库检索带用户头）每次现连，请求结束 `close_transient()` 关闭。
- **`core/llm/mcp_manager.py` — `BareNameMCPClient`**：AgentScope 2.0 默认把工具名改写为 `mcp__<server>__<tool>`，该子类还原为服务器侧裸名（`internet_search` 而非 `mcp__internet_search__internet_search`），保证展示名映射（`core/config/display_names.py`）、[引用溯源](chat.md)的按工具名分发、前端图标渲染等 1.x 约定继续成立。

`Toolkit` 在 2.0 是一次性构造，由 `core/llm/agent_factory.py` 统一执行 `Toolkit(tools=[...], mcps=clients)`。

## 配置注册：DB 驱动 + catalog 门控

MCP Server 的配置真源是数据库表 `admin_mcp_servers`（ORM：`core/db/models.py::AdminMcpServer`），由 `core/services/mcp_service.py::McpServerConfigService` 以 30 秒 TTL 缓存读出，格式与旧 `MCP_SERVERS` dict 兼容（`transport / command / args / env / url / headers / is_stable`）。`core/config/mcp_config.py` 保留为内置 Server 的 URL 构造器（首次部署种子）。

是否**对模型可见**还要过一道 [catalog](catalog.md) 门控：`core/config/catalog.json` 中 `mcp` 段的每项对应一个 server_id，`is_enabled(id, "mcp_server")` 为 false 的 Server 即使连接着也不会注册给智能体。

## 管理员自定义 MCP

管理台「MCP 工具管理」对应 `api/routes/v1/admin_mcp_servers.py`（前缀 `/v1/admin/mcp-servers`），能力包括：

- **CRUD**：新建/编辑任意 transport（`stdio` / `streamable_http` / `sse`）的 Server，支持 `command+args`（stdio）或 `url+headers`（HTTP/SSE）、环境变量注入（`env_vars` 明文 + `env_inherit` 继承宿主）、图标与用户简介；
- **创建即试连**：`_probe_connectivity` 真实连一次，失败拒绝落库；
- **开关与排序**：`POST /{id}/toggle` 即时启停（联动刷新 catalog 与连接池）；
- **密钥脱敏**：列表接口对 `env_vars` 中疑似密钥的值打码返回；
- **测试与重载**：`POST /{id}/test` 单独试连；`POST /reload-pool` 热重建连接池。

## 用户自助 MCP（能力中心）

普通用户可在能力中心添加**仅自己可见**的远程 MCP（`api/routes/v1/me_capabilities.py`，前缀 `/v1/me`）：

- `POST /v1/me/mcp-servers`：添加私有远程 MCP，**仅支持 HTTP/SSE**——用户入口禁止 stdio，避免在服务器执行任意命令；创建即试连，连不上不落库；
- `DELETE /v1/me/mcp-servers/{id}`：删除自己的私有 MCP。

实现上复用同一张 `admin_mcp_servers` 表：`owner_user_id` = 当前用户实现 owner 隔离，server_id 自动生成 `umcp_<hex>` 防冲突，`is_stable=False` 不进 warmup 池。该功能由管理台的 per-user 权限位 `can_add_mcp` 控制（社区版单租户默认即可放开；商业版由组织管理员按用户授予，见[版本说明](../editions/overview.md)）。

## 本地调试

每个 Server 都可以脱离容器单独运行（默认 stdio transport）：

```bash
# 以 stdio 方式跑单个 server（配合 MCP Inspector 等客户端调试）
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp.server

# 以 streamable-http 方式跑（模拟容器内形态）
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp.server \
  --transport streamable-http --port 9102

# 不出网自检（验证导入、工具签名）
PYTHONPATH=src/backend python -m mcp_servers.internet_search_mcp._selftest

# 容器内整体健康检查（launcher 起的最低端口）
curl -fsS http://localhost:9100/mcp/
```

修改 MCP 代码后重建容器：

```bash
docker-compose up -d --build mcp
```

## 相关源码

| 路径 | 说明 |
|---|---|
| `src/backend/mcp_servers/<name>_mcp/` | 各 MCP Server（server.py / impl / _selftest） |
| `src/backend/mcp_servers/_launcher.py` | mcp 容器入口：多进程拉起 + 崩溃重启 |
| `src/backend/mcp_servers/_serve.py` | stdio / streamable-http 双 transport 统一入口 |
| `src/backend/mcp_servers/_ports.py` | server_id → 端口映射唯一真源 |
| `src/backend/core/llm/mcp_pool.py` | MCP 连接池（stdio 池化 / HTTP per-request） |
| `src/backend/core/llm/mcp_manager.py` | MCPClient 构造 + 工具裸名还原 |
| `src/backend/core/services/mcp_service.py` | DB 驱动的 Server 配置服务（30s 缓存） |
| `src/backend/core/config/mcp_config.py` | 内置 Server URL 构造（http://mcp:NNNN/mcp/） |
| `src/backend/core/config/catalog.json` | 能力目录：MCP 启停门控种子 |
| `src/backend/api/routes/v1/admin_mcp_servers.py` | 管理员自定义 MCP API |
| `src/backend/api/routes/v1/me_capabilities.py` | 用户自助私有 MCP / 技能 API |
| `docker/Dockerfile.mcp` | mcp 容器镜像（matplotlib/openpyxl/pandoc/中文字体） |

相关文档：[能力目录](catalog.md) · [技能系统](agent-skills.md) · [知识库](knowledge-base.md) · [版本与许可](../editions/overview.md)
