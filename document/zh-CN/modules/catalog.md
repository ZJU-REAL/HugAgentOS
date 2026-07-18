# 能力目录（Capability Center）

> 最后更新：2026-06-11

能力目录（catalog）是 HugAgentOS 中"哪些能力可用"的**单一真源**：技能（skills）、子智能体（agents）、MCP 工具（mcp）、知识库（kb）四类能力都登记在 catalog 里，从 MCP 工具加载、系统提示词拼装到前端能力中心展示，全部经由它门控。在系统级默认之上，每个用户还有自己的覆盖层（catalog_overrides）与自助添加的私有能力（owner 隔离）。

## 体系结构

```
core/config/catalog.json            系统级单一真源（admin 启停 + 展示顺序）
        │  load_catalog()：TTL 缓存 + 动态源同步（技能/MCP 自动发现、陈旧项清除）
        ▼
core/config/catalog.py              公共 API：get_catalog / is_enabled / get_enabled_ids
        │                                      set_enabled / reorder_items
        ▼
core/config/catalog_resolver.py     合并层：catalog.json 默认 ∩ 用户 DB 覆盖
        │  resolve_all_runtime_enabled(db, user_id) → (skills, agents, mcps)
        ▼
请求链路消费：
  api/routes/v1/chats.py → core/chat/context.py   写入 workflow context
  core/llm/agent_factory.py                       决定连接哪些 MCP、注册哪些技能
  api/routes/v1/catalog.py                        /v1/catalog 给前端能力中心
```

### catalog.json：单一真源

文件位于 `src/backend/core/config/catalog.json`（路径可用 `CATALOG_PATH` 环境变量覆盖）。每项的最小可审计 schema：

```json
{
  "id": "internet_search",
  "kind": "mcp_server",          // tool_bundle | subagent | mcp_server | knowledge_base
  "name": "联网搜索",
  "description": "…",
  "enabled": true,
  "version": "1",
  "config": {}
}
```

`catalog_loader.py` 在加载时做**动态源同步**：从技能 loader（`core/agent_skills/`）与 MCP 配置服务（`core/services/mcp_service.py`，DB 表 `admin_mcp_servers`）发现新 id 自动补入、名称/描述/版本随源刷新、源中已消失的陈旧项自动移除；展示详情（能力中心介绍 markdown、图标）属运行时字段，不落盘。结果带短 TTL 内存缓存，写操作调 `invalidate_catalog_cache()` 失效。

### 门控 API

```python
from core.config.catalog import is_enabled, get_enabled_ids

is_enabled("mcp", "internet_search")   # 单项判定
get_enabled_ids("mcp")                 # 某类全部启用 id
```

两者是全系统的能力开关读取口：`agent_factory.py::_effective_mcp_server_keys()` 用「DB 全部启用服务 ∩ 请求级 enabled_mcp_ids（缺省时退回 `get_enabled_ids("mcp")`）∩ AgentSpec 白名单」三层求交决定本次连接哪些 MCP server；技能注册缺省取 `get_enabled_ids("skills")`。改 catalog 即时影响下一次请求，无需重启。

## 用户级覆盖（catalog_overrides）

用户在能力中心里开/关某项能力，写入 DB 表 `catalog_overrides`（`core/services/catalog_service.py`），按用户独立、互不影响：

- **合并算法**（`catalog_resolver.py::_merge_kind`）：覆盖只能翻转 base catalog 中**已存在**项的启用位，不能复活已删除项。
- **管理员锁**：base catalog 中 `enabled=false` 的项，用户覆盖**不可**重新启用，且在 `/v1/catalog` 响应中对前台完全隐藏。
- 解析结果按 user_id 缓存 30 秒（`resolve_all_runtime_enabled`）。

每次对话时 `core/chat/context.py::resolve_enabled_capabilities()` 把合并结果写入 workflow context（请求体显式传入的列表优先）。

## /v1/catalog 路由与 KB 注入

`api/routes/v1/catalog.py`：

- `GET /v1/catalog`：返回四类能力的**用户视角**合并结果（base + 用户覆盖 + 用户私有项），前端 `src/frontend/src/api.ts::getCatalog()` 调用。
- `PATCH /v1/catalog/{kind}/{id}`：写用户覆盖（kind 取 `skill / agent / mcp / kb`；kb 仅运行时开关不落库）。技能/工具变更会级联失效系统提示词缓存。

**KB 项为运行时注入**，不持久化在 catalog.json：

| 来源 | 条件 | 标记 |
|---|---|---|
| Dify 外部知识库 | `KNOWLEDGE_BASE=dify` 且凭据可用（`core/kb/dify_kb.py::is_dify_enabled`），数据集列表带 60s 进程缓存 | `visibility: public`（**商业版 EE**：对接外部 Dify 知识库） |
| 公共自建知识库 | 管理台「知识库管理」创建（本地 Milvus），所有用户可见、前台只读 | `visibility: public` |
| 用户私有知识库 | 当前用户的本地 KB 空间 | `visibility: private` |

详见 [知识库](knowledge-base.md)。

## 用户自助能力（me_capabilities）

`api/routes/v1/me_capabilities.py` 让用户**不经管理员**给自己接入能力，是社区版的核心自助路径。所有自建项 `owner_user_id = 当前用户`，仅本人可见可用（owner 隔离）：

| 端点 | 功能 |
|---|---|
| `POST /v1/me/mcp-servers` | 添加私有远程 MCP（仅 `streamable_http` / `sse`，**不支持 stdio**——不允许在服务器执行任意命令）；创建即试连，连不上拒绝落库 |
| `DELETE /v1/me/mcp-servers/{id}` | 删除自己的私有 MCP |
| `POST /v1/me/skills/upload` | zip 包上传私有技能（≤50MB，复用 admin 解析链路） |
| `POST /v1/me/skills` | 手写新建/更新私有技能（SKILL.md 正文 + 元数据） |
| `GET/DELETE /v1/me/skills/{skill_id}`、`PUT .../icon` | 读取编辑、删除、设图标 |

**权限位**：每个端点先检查 `users_shadow.metadata` 中的布尔位——`can_add_mcp`、`can_add_skill`（个人 API-Key 同机制用 `can_use_api_key`，见 [模型接入](model-providers.md)）。这些权限位由 Config 管理平台的用户管理模块按用户授予：社区版单租户下默认放开即得完整自助；**按用户授予的组织级治理与技能上架审核为商业版（EE）**。

运行时隔离：`agent_factory.py` 会把当前用户的私有 MCP 合入可连接集合（`get_owned_servers`），并用 `_filter_skill_ids_for_user()` 剔除属于他人的私有技能 id，防止越权调用；`/v1/catalog` 把私有项以 `owner: "self"`、`deletable: true` 注入该用户的响应。

## 前端能力中心（components/catalog/）

| 组件 | 职责 |
|---|---|
| `AbilityCenterPage.tsx` | 能力中心主页：四类能力浏览、详情、启停、自助添加入口 |
| `CatalogPanel.tsx` | 会话内能力面板（本次对话启用哪些技能/工具/KB） |
| `McpPage.tsx` / `SkillsPage.tsx` | MCP / 技能分页与管理 |
| `SkillMarketplaceModal.tsx` | 技能市场浏览安装（详见 [技能系统](agent-skills.md)） |
| `SkillIconPicker.tsx` / `skillIcons.tsx` | 图标选择与预设 |

状态集中在 `src/frontend/src/stores/catalogStore.ts`，本地默认值在 `storage.ts::defaultCatalog`。

## 管理端能力管理

- **`/admin` 内容管理台**：技能上传/启停/排序（`api/routes/v1/admin_skills.py`，内部调 `catalog.set_enabled` / `reorder_items` 回写 catalog.json）、MCP server 管理（`admin_mcp_servers.py`，DB 表 + 试连 + 缓存刷新）、子智能体管理（`admin_agents.py`）、技能市场上架审核（`admin_marketplace.py`，**商业版 EE**）。
- **catalog 快照迁移**：`scripts/export_content.py --only catalog` 导出 catalog.json + `catalog_overrides`，`scripts/import_content.py --catalog <snapshot>` 导入。

## 相关源码

| 主题 | 路径 |
|---|---|
| 公共 API（is_enabled 等） | `src/backend/core/config/catalog.py` |
| 单一真源文件 | `src/backend/core/config/catalog.json`（`CATALOG_PATH` 可覆盖） |
| 加载 / 动态同步 / 缓存 | `src/backend/core/config/catalog_loader.py`，`catalog_common.py`，`catalog_migration.py` |
| 用户覆盖合并 | `src/backend/core/config/catalog_resolver.py`，`core/services/catalog_service.py` |
| 目录路由 | `src/backend/api/routes/v1/catalog.py` |
| 用户自助能力 | `src/backend/api/routes/v1/me_capabilities.py` |
| MCP 服务配置（DB） | `src/backend/core/services/mcp_service.py`，`api/routes/v1/admin_mcp_servers.py` |
| 技能管理 | `src/backend/api/routes/v1/admin_skills.py`，`core/agent_skills/` |
| Dify KB 注入 | `src/backend/core/kb/dify_kb.py` |
| 前端能力中心 | `src/frontend/src/components/catalog/`，`stores/catalogStore.ts` |
| 工厂消费侧 | `src/backend/core/llm/agent_factory.py::_effective_mcp_server_keys` |
