# MCP 工具系统

> 最后更新：2026-08-11

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
│  agent) │        │        │   :9103  产业知识中心统一 MCP（插件，EE）       │
└─────────┘        │        │   :9104  generate_chart_tool（图表生成）          │
     │             │        │   :9105  保留（原报告导出 MCP）                  │
 MCPConnectionPool │        │   :9106  web_fetch（网页抓取）                    │
 (core/llm/        │        │   :9107  batch_runner（批量计划）                 │
  mcp_pool.py)     └───┬────┘   :9108  automation_task（自动化插件提供）      │
                       │        9109–9111 保留（原 excel/ppt/pdf MCP）        │
                       │        :9112  skill_manager（技能管理）              │
                       └──────────────────────────────────────────────────────┘
```

端口分配的唯一真源是 `src/backend/mcp_servers/_ports.py`：`core/config/mcp_config.py`（后端拼 `http://mcp:NNNN/mcp/` URL）和 `mcp_servers/_launcher.py`（容器内绑定端口）都从这里读取。

> 历史说明：办公文档编辑与导出（word / excel / ppt / pdf）已整体迁出
> `mcp` 容器，改为以 [Agent 技能](agent-skills.md)（word-editing /
> excel-editing / ppt-design / pdf-editing）的形态在沙箱容器内执行，各技能
> 自带引擎。因此 `report_export_mcp` 已退役，9105 保留但不再启动；9108
> 由自动化插件提供，9109–9111 仍保留。

## 内置 MCP Server 一览

| Server（目录） | 端口 | 工具 | 版本 |
|---|---|---|---|
| `retrieve_dataset_content_mcp` | 9100 | `retrieve_dataset_content` / `list_datasets` / `retrieve_local_kb` | 社区版 CE |
| `query_database_mcp` | 9101 | `query_database` | **商业版 EE** |
| `internet_search_mcp` | 9102 | `internet_search` | 社区版 CE |
| `ai_chain_information_mcp` | 9103 | 27 个产业链、企业、资讯、政策、报告、专利与技术工具 | **商业版 EE（产业知识中心插件）** |
| `generate_chart_tool_mcp` | 9104 | `generate_chart_tool` | 社区版 CE |
| `web_fetch_mcp` | 9106 | `web_fetch` | 社区版 CE |
| `batch_runner_mcp` | 9107 | `batch_plan` | 社区版 CE |
| `automation_task_mcp` | 9108 | `create_scheduled_task` / `list_scheduled_tasks` / `update_scheduled_task` 等 | 社区版 CE（自动化插件提供） |
| `skill_manager_mcp` | 9112 | `search_marketplace` / `install_from_marketplace` / `register_skill` / `list_my_skills` / `submit_to_marketplace` / `delete_skill` | 社区版 CE |

> 版本边界以 [开源与商业化产品方案](../editions/overview.md) 为准：依赖行业数据源的
> 产业知识中心插件与数据仓库查询属于商业版。社区版派生树通过 `ce/manifest.yaml`
> 物理剔除产业插件的 1 个自包含 MCP 和 14 个 Skills；通用工具进入社区版。

### retrieve_dataset_content — 知识库检索（CE）

知识库 RAG 的检索入口，一个 Server 暴露三个工具：

- **`retrieve_dataset_content(query, dataset_id, top_k, score_threshold, search_method, reranking_enable, weights)`**：对接 Dify 外部知识库的语义/混合检索；
- **`list_datasets()`**：列出当前用户可用的全部知识库（公有 + 私有），含名称、简介与文档列表，供模型先探查再检索；
- **`retrieve_local_kb(kb_id, query, top_k)`**：检索平台自建私有知识库。

它是唯一的 **per-request** Server：每次对话请求按当前用户把允许访问的知识库 ID、用户 ID、重排序开关通过 **HTTP 请求头**（`X-Allowed-Dataset-Ids` / `X-Allowed-Kb-Ids` / `X-Current-User-Id` / `X-Reranker-Enabled`）注入（见 `core/llm/agent_factory.py::_apply_runtime_kb_constraints`），Server 端用 `ctx.request_context` 读头实现多用户隔离。详见[知识库模块](knowledge-base.md)。

### query_database — 数据仓库查询（商业版 EE）

`query_database(question, 工号)`：把用户的完整自然语言问题整体传给内网数据仓库服务，由其内部完成问题分解、多表联查与 NL2SQL，返回可核对的精确指标数值（如规上工业增加值、增速、利润总额）。工具描述中将其定为"精确数值类问题的最高优先级数据源"。**完全依赖内网数仓，无法脱离行业数据运行，属商业版。**

### internet_search — 联网搜索（CE）

`internet_search(query, max_results, topic, search_depth, include_raw_content,
cn_only)`：通过 `INTERNET_SEARCH_ENGINE` 选择 Tavily、百度或 LangSearch，
并且只读取所选引擎对应的 API Key。三种引擎的结果统一为
`title / url / content`；`topic`、`search_depth` 和原始正文参数仅由 Tavily
原生支持，LangSearch 会将搜索摘要映射到 `content`。该工具是兜底工具，
仅在用户配置的知识库和其他专业工具都没有结果时使用。

### 产业知识中心插件（商业版 EE）

产业知识中心不再作为一个不可卸载的静态 MCP 提供，而是作为原生插件进入插件市场。
插件一次安装 1 个 MCP Server 和 14 个工作流 Skills；启停、更新和卸载统一跟随插件
生命周期。升级数据库会自动安装一次插件并移除旧静态 MCP 行；管理员后续主动卸载后，
启动不会再次恢复。

统一 MCP 暴露 27 个高价值工具：

| 组件 | 工具数 | 能力 |
|---|---:|---|
| `ai_chain_information_mcp`（9103） | 27 | 13 个成熟兼容工具；14 个产业目录/简报/竞争力、企业筛选评价、政策/报告、专利和技术路线工作流 |

统一 MCP 包直接保存显式工具函数、详细中文说明、固定接口组合和包内 `common.py`，
不使用集中式动态注册，也不存在根级产业公共模块。客户端在每次调用时读取
`industry.url` 和 `industry.auth_token`。管理员仍在**系统配置**或插件详情的管理员配置区
维护这两个值；插件不声明 `required_secrets`，普通用户安装时不输入 URL 或 Token。

新增工具提供扁平参数，并在智能体可见描述中完整说明适用场景、参数来源、输出板块和相邻
工具选择。14 个新增工作流的结果只保留 `结果`、可选的 `未获取内容` 和截断时的 `结果说明`；
13 个成熟工具保留既有业务输出结构。商业版 EE 前端提供产业链 Canvas、资讯列表和企业画像
专用渲染器。CE 派生树物理移除这些渲染器、行业 API 客户端和配套资源；用户自行接入的远程
MCP（即使工具同名）也统一使用通用 JSON 卡片展示。两类结果都不会把接口路径、HTTP 状态与
执行计数带入回答上下文。多接口工作流并发执行并保留部分成功结果，业务数据仍设置 25,000
字符安全上限。

商业版 EE 的产业链 Canvas 会读取 `get_chain_information` 图谱中的真实节点 ID。点击末级节点后，前端通过
受登录与 `industry_tools` 许可保护的后端接口分页加载该节点关联企业，展示企业名称、资质标签、
所属地区、成立日期和注册资本；上游地址与 Token 不会发送到浏览器。智能体侧也可直接调用
`ikc_screen_enterprises`：不传筛选条件时列出节点全部企业，传入地区、年限、资本或标签时进行筛选。
企业面板打开时，图谱会以较大比例聚焦当前末级节点，并保留少量上游链路作为上下文，不会为完整
展示全部节点而过度缩小；关闭面板后恢复原视图。展开分支后，画布会自动聚焦新增节点，避免内容
落到可视区域之外。企业列表中的每一行都可在新标签页打开对应的上游企业详情页。

内部仍审计页面发现的 240 条接口，但能力目录、任意接口调用、地区树、通用实体解析、个人
资料库、收藏订阅、上游报告工作台和写入操作均不再暴露给智能体。

### generate_chart_tool — 数据可视化（CE）

`generate_chart_tool(data, query)`：接收 JSON 数据与绘图指令，用 matplotlib 渲染折线图/柱状图/饼图等（mcp 容器内置文泉驿 + 方正中文字体保证 CJK 渲染），图片保存为平台 artifact 并返回 `file_id` / 下载 URL。工具描述强制要求"先用数据查询工具拿到真实数据再绘图"，并给出了与沙箱协作的标准链路（`sandbox_put_artifact` 把图表拷入沙箱后再插入 Word/PPT）。

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

## 工具引用声明（`__citations__`）

平台的[引用系统](chat.md)（证据锚点）会在每个工具结果回给模型前自动提取可引用条目、
分配全会话唯一锚点（`e1`、`e2`、…）并把 `cite_id` 回注进结果——**任何工具默认可引用**，
不写一行引用代码也能工作。但提取粒度取决于后端认不认识你的返回结构，因此开发新工具
（自研工具或 MCP tool）时遵循以下优先级：

1. **结果需要被引用、且希望精确控制粒度 → 返回 JSON 里带 `__citations__` 字段（推荐）**：

   ```json
   {
     "result": "……业务数据本体……",
     "__citations__": [
       {"title": "来源标题", "url": "https://…", "snippet": "关键摘录", "source_type": "internet"},
       {"title": "第二个来源"}
     ]
   }
   ```

   - 条目顺序与结果正文对应（`item_index` 按声明顺序记录）；
   - `title` 必填倾向（缺省回退工具显示名），`url` / `snippet` / `source_type` 可选；
   - 中间件（`CitationAnchorMiddleware`）会**优先采用**该声明，就地为每条注入
     `"cite_id": "eN"`，模型引用时原样复制。
2. **标准列表结构 → 加一行注册表配置**：结果形如 `{"items": [{"title": …, "content": …}]}`
   的列表型工具，在 `orchestration/citation_anchor.py::TOOL_SPECS` 里登记
   `items_paths` + 字段别名即可逐条编号，不改工具代码。
3. **什么都不做 → 自动兜底**：通用启发式能识别顶层（或 `result` 下一层）唯一的
   字典数组字段并逐条编号；彻底认不出时整份结果作为 1 个锚点。
4. **操作型工具（写文件 / 发布 / 增删改回执）→ 加进 `SKIP_TOOLS`**：这类结果没有
   引用价值，登记跳过名单可免去无意义的锚点噪音。

注意：`__citations__` 与 `cite_id` 是**平台层约定**，不是 MCP 协议字段；第三方 MCP
Server 同样适用（返回 JSON 即可）。回注发生在结果进模型上下文与落库之前，前端工具卡片
会把 `cite_id` 渲染成条目徽章，与正文 `[锚文本](cite:eN)` 引用一一对应。

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

## 系统配置管理员自定义 MCP

`/config` 系统配置台的「MCP 工具 → MCP 服务管理」对应 `api/routes/v1/admin_mcp_servers.py`（前缀 `/v1/admin/mcp-servers`），使用 `CONFIG_TOKEN` 或 `can_system_config` 权限，能力包括：

- **CRUD**：新建/编辑任意 transport（`stdio` / `streamable_http` / `sse`）的 Server，支持 `command+args`（stdio）或 `url+headers`（HTTP/SSE）、环境变量注入（`env_vars` 明文 + `env_inherit` 继承宿主）、图标与用户简介；
- **创建即试连**：`_probe_connectivity` 真实连一次，失败拒绝落库；
- **开关与排序**：`POST /{id}/toggle` 即时启停（联动刷新 catalog 与连接池）；
- **密钥保护**：HTTP Header 值加密存储、接口统一以 `***` 返回；`env_vars` 中疑似密钥的值同样脱敏；
- **测试与重载**：`POST /{id}/test` 单独试连；`POST /reload-pool` 热重建连接池；
- **移至 MCP 市场**：把现有远程 HTTP/SSE MCP 生成无凭据的市场快照，
  并停用原全局实例；该服务随后从 MCP 服务管理列表移除，可在市场中复核、
  编辑展示信息，再按需全局安装。原服务已配置的 Token 仍加密保留在服务端，
  作为管理员托管凭据供获准用户安装，市场记录和前端响应都不包含凭据值。

插件提供的 MCP（例如自动化插件的定时任务 MCP）不在 MCP 服务管理列表中
展示。它们的启停和卸载跟随插件生命周期，统一在插件管理入口操作。

## 用户自助 MCP（能力中心）

普通用户可在能力中心添加**仅自己可见**的远程 MCP（`api/routes/v1/me_capabilities.py`，前缀 `/v1/me`）：

- `POST /v1/me/mcp-servers`：添加私有远程 MCP，支持公网 HTTP/HTTPS 上的 HTTP/SSE——用户入口禁止 stdio，避免在服务器执行任意命令；地址会经过 DNS/IP 检查以阻断 localhost、内网、链路本地与保留地址；创建即试连，连不上不落库。HTTP 为明文传输，生产环境仍建议使用 HTTPS；
- `DELETE /v1/me/mcp-servers/{id}`：删除自己的私有 MCP。

实现上复用同一张 `admin_mcp_servers` 表：`owner_user_id` = 当前用户实现 owner 隔离，server_id 自动生成 `umcp_<hex>` 防冲突，`is_stable=False` 不进 warmup 池。HTTP Header 值使用 Fernet 加密后落库，运行时才解密。该功能由管理台的 per-user 权限位 `can_add_mcp` 控制（社区版单租户默认即可放开；商业版由组织管理员按用户授予，见[版本说明](../editions/overview.md)）。

## MCP 市场

系统级市场治理入口位于 `/config → MCP 工具`。与技能管理一致，MCP 服务列表上方直接提供「MCP 市场」和「上架审核」两个按钮并以弹窗承载，不再增加第二层管理页签；`/admin` 内容运营台也不提供重复入口。这里可以上架和全局安装市场条目、审核用户申请、设置可见范围、复检、封禁和移除条目，使用 `CONFIG_TOKEN` 或 `can_system_config` 权限。

能力中心的「MCP 市场」与技能、子智能体、插件市场使用同一套可见范围模型，支持公开、指定用户、团队与角色授权。市场条目本身只保存**无凭据的、已审核版本快照**；管理员发布的条目可以在源 `admin_mcp_servers` 上保留管理员托管凭据，用户安装时由后端加密复制到个人实例，凭据值不会进入市场表或返回前端：

- 用户浏览条目详情、工具列表、输入 Schema 和风险等级，并一键安装为仅自己可见的私有 MCP；若市场认证策略选择“管理员统一配置”且已保存完整 Token，界面显示“管理员托管”并免填安装；选择“用户自行填写”时，每个用户安装时填写自己的 Token 或完成 OAuth；
- 用户可以把自己已经试连成功的私有 MCP 提交上架，查看待审、通过、驳回状态，并在审核前撤回；从 MCP 市场安装的实例不会再次提供申请入口，后端也拒绝重复上架；
- 管理端新建远程 HTTP/SSE MCP 时可勾选“安装时需要 Token/Auth”，并选择 Token 或 OAuth。Token 可由管理员直接填写，也可留空：填写后用户安装免填，留空后每个用户自行填写；OAuth 由每个用户安装时授权。无管理员凭据导致首次连接返回未授权时，条目仍可先上架，并在用户完成认证后按 `per_install` 模式动态发现工具。远程 MCP 不会直接全局生效；StdIO 仍直接受全局服务管理，跨环境分发必须走插件市场；
- 管理员可以编辑市场条目的名称、简介、用户提示、分类、标签、图标与认证策略。认证策略明确分为“无需认证”“用户自行填写”“管理员统一配置”；统一 Token 也只在编辑窗口维护，市场列表不再提供“更新全局凭据”。展示信息与管理员统一 Token 的更新会同步到现有安装实例。已审核的端点、工具 Schema、风险报告和版本号仍是版本快照；
- 管理端支持上架/下架、按用户/团队/角色控制可见范围、人工复检、软删除，以及安全封禁；人工复检确认真实工具变化时，会保留旧快照并自动生成新的补丁版本后恢复安装。若原始 MCP 连接记录已被清理，系统会改用已审核版本中的无凭据端点复检，快照一致时直接恢复安装；封禁会立即停用该条目派生出的所有安装实例，解除封禁时恢复这些实例；
- 工具名或输入 Schema 命中删除、执行、写入等语义时会标记中高风险，高风险安装需要再次明确确认；
- 后端默认每 6 小时重新连接远程 MCP 并比较工具快照哈希。发生漂移时条目进入 `changed`，暂停新安装，等待管理员复核；定时任务只检测变化，不会自动接受新快照。周期可用 `MCP_MARKET_REVALIDATE_INTERVAL` 调整。

新部署会一次性加入五个**平台精选模板**，它们只出现在市场中，不会自动安装或启用：

| 条目 | 用户安装时填写 | 说明 |
|---|---|---|
| 高德地图 MCP | 高德 Web 服务 API Key | Key 加密保存，运行时才注入 URL 的 `key` 查询参数；市场 URL 永不包含 Key。 |
| 秘塔搜索 MCP | 秘塔 API Key | 运行时自动组装 `Authorization: Bearer …`。 |
| GitHub MCP | Fine-grained PAT，或 OAuth App 登录 | GitHub 官方远程 MCP 不支持 DCR；OAuth 方式需填写自行注册的 Client ID/Secret。 |
| GitLab MCP | 浏览器 OAuth（推荐），或带 `mcp` scope 的 access token | GitLab 官方 MCP 仍为 Beta；浏览器方式支持 DCR、PKCE 和刷新令牌，普通 PAT 可能无法连接。 |
| 阿里云可观测 MCP | 已配置好的个人 SSE 地址 | 按阿里云安全建议先在 ModelScope Hosted、函数计算或受保护环境配置 AK/SK；平台不直接接收云 AK/SK。 |

精选模板使用 `per_install` 工具发现：市场详情展示官方能力示例，安装时再用当前用户的凭据连接官方端点、读取实际工具 Schema、重新评估风险并保存到该用户的私有 MCP。GitHub、GitLab 这类含写操作的模板预先标为高风险，必须明确确认后才能安装。

### 通用认证范式

每个市场版本通过 `auth_config` 声明一个或多个认证方法，统一支持 `none`、`token` 与 `oauth2`；`credential_mode` 指定凭据由 `installer`（每个用户）还是 `admin`（管理员统一）提供；`auth_schema` 只描述安装时需要采集并注入 Header、查询参数或个人端点的字段，并可用 `methods` 限定字段属于哪种方法。旧条目没有显式策略时会按是否存在认证字段与管理员源凭据自动推断，保证向后兼容。

管理员在新增或市场编辑窗口选择“管理员统一配置”后，后端会检查源服务是否保存全部必填凭据。满足时，列表与详情仅返回 `credentials_managed_by_admin=true`，安装接口在服务端复用这些值并重新加密到具体安装实例；任何 Token 内容都不会出现在响应中。选择“用户自行填写”后，即使源服务仍保留用于复检的 Token，市场安装也不会复用它。社区用户提交的 MCP 与平台精选模板仍坚持安装者独立认证，不复用发布者凭据。

OAuth 远程 MCP 按 MCP 授权规范执行受保护资源元数据与授权服务器元数据发现、Authorization Code + PKCE、`state` 校验、RFC 8707 resource 参数，并在提供 DCR 时动态注册客户端；不支持 DCR 的服务由安装界面采集 Client ID/Secret。OAuth 的 MCP 端点、授权元数据地址与回调基址均允许公网 HTTP/HTTPS，但 HTTP 会明文传输授权码和 Token，仅建议用于受控测试环境。access token、refresh token、客户端信息与过期时间作为一个加密 bundle 只存入具体安装实例，运行时自动刷新，任何 OAuth 凭据都不会进入市场条目或工具快照。

同源部署会自动使用浏览器当前站点的 `/api/v1/mcp-market/oauth/callback`；前后端跨域部署必须设置 `MCP_OAUTH_PUBLIC_BASE_URL` 为浏览器可访问的 API 基址，避免从请求 `Host` 反射不可信回调地址。

用户接口前缀为 `/v1/mcp-market`，覆盖列表、详情、私有安装、提交和撤回；系统配置管理接口为兼容现有客户端保留 `/v1/admin/mcp-market` 前缀，覆盖发布、全局安装、审核、可见范围、复检、封禁和移除，但鉴权归属 `/config` 的 `CONFIG_TOKEN` / `can_system_config`。为避免远程代码执行风险，市场只接收 `streamable_http` / `sse` 远程 MCP；需要 stdio 的能力应随已审核插件发布，由插件安装链路管理其代码和依赖。

### 数据边界

市场使用四张独立表：`mcp_market_items` 保存条目元数据，`mcp_market_versions` 保存不可变工具快照、风险报告和无密钥认证契约，`mcp_market_submissions` 保存审核申请快照，`mcp_market_installations` 关联条目版本和实际 MCP 实例。任何 Header、Token、OAuth bundle、查询参数 Key 或个人端点都不会进入市场条目、版本或申请。社区条目与精选模板由安装者独立认证；管理员发布的 Token 条目可从源 `admin_mcp_servers` 读取加密托管凭据，并把凭据重新加密保存到具体安装实例，全程不向前端暴露。

用户市场、认证与运行时刷新位于 `core/` 和 CE 路由，是社区版能力；`/config` 管理端路由与 `components/admin` 仍由 EE 注册表和 CE 派生清单物理剔除。主仓使用 `mcpmkt01`～`mcpmkt04retire` 迁移链，CE 通过独立的 `ce_0003` 与 `ce_0004` 迁移获得相同核心表和退役清理，不依赖任何 `edition_ee` 实现。

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
| `src/backend/core/services/mcp_marketplace_service.py` | 市场发布、审核、安装、可见性与安全控制 |
| `src/backend/core/services/mcp_oauth_service.py` | OAuth 2.1 登录、SDK 元数据发现、加密令牌存储与刷新 |
| `src/backend/core/services/mcp_marketplace_monitor.py` | 远程工具快照定期复检与漂移监控 |
| `src/backend/core/config/mcp_config.py` | 内置 Server URL 构造（http://mcp:NNNN/mcp/） |
| `src/backend/core/config/catalog.json` | 能力目录：MCP 启停门控种子 |
| `src/backend/api/routes/v1/admin_mcp_servers.py` | 管理员自定义 MCP API |
| `src/backend/api/routes/v1/me_capabilities.py` | 用户自助私有 MCP / 技能 API |
| `src/backend/api/routes/v1/mcp_marketplace.py` | 用户端 MCP 市场 API |
| `src/backend/api/routes/v1/admin_mcp_marketplace.py` | 管理端 MCP 市场与审核 API |
| `docker/Dockerfile.mcp` | mcp 容器镜像（MCP 运行时、绘图依赖与中文字体） |

相关文档：[能力目录](catalog.md) · [技能系统](agent-skills.md) · [知识库](knowledge-base.md) · [版本与许可](../editions/overview.md)
