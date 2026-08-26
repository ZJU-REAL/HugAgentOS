# 提示词系统

> 最后更新：2026-08-24

HugAgentOS 的系统提示词不是写死的字符串，而是一套**DB 优先、文件兜底、版本化管理**的装配系统：运行时从数据库读取激活版本的分段（parts）拼装主智能体提示词，管理员可在 Config 管理台维护多个版本并一键激活，全部内容可作为快照在环境间迁移。文件系统中的 markdown 仅作为首次部署的种子和 DB 不可用时的兜底。

## 装配机制（prompts/prompt_runtime.py）

`build_system_prompt(config, ctx)` 是主智能体系统提示词的唯一入口，由 `core/llm/agent_factory.py` 在每次建 agent 时调用。装配优先级：

```
1. 版本池激活版本           ContentBlock(id="prompt_versions") 中 kind="system" 的 active 版本
   （AdminPromptPart 表的同 part_id 行可覆盖单段内容，兼容旧管理 UI 的数据）
2. 文件系统 parts           prompts/prompt_text/default/system/*.system.md
   （provider="filesystem"，目录可被 PROMPT_DIR 环境变量覆盖）
3. 内联模板                 provider="inline" / PROMPT_INLINE_TEMPLATE
4. 硬编码最小提示            prompts/provider.py::hardcoded_minimal_system_prompt()
                            —— 保证永不为空
```

兜底分段（DB 为空时生效）共 5 个文件，按文件名序拼接：

```
src/backend/prompts/prompt_text/default/system/
├── 00_role.system.md          # 角色定位
├── 10_constraints.system.md   # 防幻觉等硬约束
├── 20_tools.system.md         # 工具使用规范
├── 30_workflow.system.md      # 工作流程
└── 40_format.system.md        # 输出格式 + [ref:工具名-N] 引用规范
```

在 base prompt 之后，运行时还会按上下文追加动态段：工具与技能说明（`_TOOLS_AND_SKILLS_NOTICE`）、轻量知识库目录（`prompts/kb_lite_section.py`）、项目模式段（`prompts/project_section.py`，仅项目对话注入）、代码执行段与批量模式提示（由 `agent_factory.py` 追加）、子智能体路由表（`core/llm/subagent_tool.py::build_subagent_prompt_section`）。

### 缓存设计

提示词装配带三层缓存，全部支持主动失效：

| 缓存 | TTL | 说明 |
|---|---|---|
| 模板缓存 `_prompt_cache` | 300s | key 含 provider、parts、DB/激活版本，以及**完整**动态上下文的 SHA-256 canonical hash（完整项目指令、完整文件清单、工具定义、MCP/KB 集合和未来新增模板变量）；key 只保存哈希，不再保存截断或明文的项目内容。`{now}` 仍以“天”为粒度替换，保持稳定前缀与模型侧缓存命中 |
| DB parts 预载 `_db_parts_preloaded` | 启动时 `warmup_prompt_cache()` 预载，写后重载 | 首个请求不查库 |
| DB 版本号 `_db_version_cache` | 30s | `MAX(admin_prompt_parts.updated_at)` 作为 cache-busting 版本串 |

任何提示词写操作（管理台编辑、版本激活、快照导入、能力开关变更）都会调 `invalidate_prompt_cache()` 级联清空并立即重热。

### 执行 Manifest 与运行时绑定

Agent 真正开始执行前，`core/llm/execution_manifest.py::PromptManifestBuilder` 会记录最终请求面。每个有序 PromptSection 都带 `id`、来源、信任级、优先级、缓存类别、预算、token 估算、内容哈希和版本；每个最终 ToolDefinition 同样对名称、完整描述、canonical 参数 schema、权限策略和恢复策略生成稳定哈希。工具定义按稳定 id 排序，提示词分段则保持刻意设计的前缀顺序。

Builder 最终产出 `prompt_hash`、`prompt_manifest_hash`、`tool_manifest_hash`、`context_hash` 和统一的 `aggregate_hash`。Manifest 构建与 AgentScope 模型请求共用同一份 run-scoped 工具/技能冻结快照，因此“记录的请求面”和“实际发送的请求面”不会错位；渐进式插件激活会让旧快照失效，并在下一次模型调用前发布明确递增的 `surface_generation`。

Runtime Binder 把这份脱敏 Manifest 挂到本轮递归不可变的 Asset Bundle，并用真实 `run_id` 建立索引；Bundle 的 memory policy ref 同时带真实 `workspace_id`。Manifest 与资产引用中的嵌套容器禁止原地修改，序列化导出则返回互不影响的可变副本。响应结束后 Evolution Episode 持久化该 Bundle，因此即使资产后来已更新，也能准确审计本轮实际请求。

提示词正文、项目指令、文件名、工具描述与 schema 都按**完整 canonical 内容**参与哈希，但不会以明文复制到 Manifest 或日志。持久化 context 只保留哈希、数量/大小，以及 workspace/project id 等公开引用。

### 统一 Context IR

每次模型调用前，`core/llm/context_ir.py` 都会把最终上下文统一转换成不可变的 `ContextItem`。用户输入、助手历史、账户身份、冻结记忆、项目/文件材料、附件、工具调用与结果、压缩摘要、执行中追加指令、Harness 系统提醒，都会携带机器可读的 `kind`、来源、信任级、可见性、优先级、单项预算、截断策略、内容引用/哈希、缓存类别和创建顺序。为兼容模型，记忆和对话中途的提醒仍可渲染成 `user` role，但系统不再靠 XML 标签或 role 猜测它们是谁说的。

`ContextAssembler` 使用确定性顺序，并设置明确的消息预算：模型窗口先预留 15% 给响应，再扣除最终工具 schema 的 canonical token 预算。关键系统规则和当前用户指令不会被低可信内容挤掉；可选内容按优先级选择；文本和工具输出按声明的头尾保留策略截断；工具调用与对应结果则作为一个整体保留、缩短或排除。脱敏 Context Manifest 会逐项说明“纳入、截断、排除”及原因，不保存正文；其哈希会进入本次请求的 `context_hash` 与 `aggregate_hash`，再重新绑定到该 run 的 Asset Bundle。

`core/llm/context_adapter.py::AgentScopeContextAdapter` 是 Context IR 与 AgentScope 消息之间唯一的转换入口。持久化会话中的来源元数据可在历史回放时保留，最终模型 role 和 SSE 行为继续兼容。`ManifestBoundAgent` 会在 run-scoped 工具/技能请求面冻结后，通过公开的 `Agent.reply` 链路完成最终装配。

## 提示词版本池（prompt_versions）

版本池把多套提示词存进单行 `ContentBlock(id="prompt_versions")`，payload 结构 `{active: {kind: version_id}, versions: [...]}`，服务层为 `core/services/prompt_version_service.py`：

- **六类 kind**（`VALID_KINDS`）：`system`（主智能体）、`code_exec`（代码执行能力段）、`distillation`（技能蒸馏）、`plan_mode`（计划模式）、`subagents`（平台默认探索员 / 执行员 / 审查员的独立提示词）、`turbo`（极速模式独立提示词）。
- 每个版本含 `(kind, id, name, description, parts[])`，part 即 `{part_id, display_name, content, sort_order, is_enabled}`。
- **API**：`list_versions / get_version / upsert_version（支持 from_id 克隆）/ delete_version（激活中禁删）/ activate_version`；激活后立即失效运行时缓存。
- **Seed**：`seed_from_filesystem()` 首次冷启动把文件系统 markdown 读成默认版本，并幂等补齐 `subagents/default` 的三个角色 part；内置两个一次性迁移——`system/v4 → system/default` 改名、从各 system 版本抽出 `system/90_plan_mode` 生成 `plan_mode/default`。
- 启动时还会幂等补种两个动态段到激活 system 版本：`system/05_system_reminder_convention`（教模型处理 `<system-reminder>` 带外信号）与项目模式段（`prompt_runtime.py::ensure_*_seeded`）。

### Config 管理台

管理入口在 Config 管理台「提示词管理」，对应路由 `api/routes/v1/admin_prompts.py`（`CONFIG_TOKEN` 鉴权）：

前端版本管理标签页仅展示 `system / turbo / plan_mode / code_exec / distillation`。`subagents` 仍由后端版本池保存并参与种子、运行时解析和跨环境快照，但不再作为 Config 可编辑标签页；三个内置角色及其只读提示词改在用户侧「子智能体」页面展示，用户只管理自己的启停状态。

| 端点 | 功能 |
|---|---|
| `GET/POST/PUT/DELETE /v1/admin/prompts/versions...` | 版本池 CRUD（按 kind） |
| `POST /v1/admin/prompts/versions/{kind}/{id}/activate` | 激活版本 |
| `GET/PUT/DELETE /v1/admin/prompts/parts/{part_id}` | 激活版本的分段编辑 |
| `PUT /v1/admin/prompts/order` | 分段排序 |
| `POST /v1/admin/prompts/preview` | 预览运行时真实拼装结果（含代码执行段与工具附录，与 agent 实际所见一致） |
| `GET/POST /v1/admin/prompts/export` / `import` | 分段级导出/导入 |

## 场景提示词

| kind | 运行时消费方 | 解析顺序 |
|---|---|---|
| `code_exec` | `agent_factory.py` 在 `CODE_CAPABILITY_ENABLED=true` 时把该段（代码能力提示词）拼到 system prompt 尾部；单一真源为 `prompt_version_service.render_code_capability_segment()`，管理台 preview 同源 | DB 激活版本 → `prompts/prompt_text/code_exec/system/*.system.md` |
| `distillation` | 技能蒸馏（`core/llm/skill_distiller.py`，把对话轨迹蒸馏为可复用技能） | DB 激活版本 → `prompts/prompt_text/distillation/skill_distiller.system.md` |
| `plan_mode` | 计划模式生成子智能体（`orchestration/subagents/plan_mode.py::_load_plan_prompt`） | DB `plan_mode` 激活版本 → 旧版 system 版本的 `system/90_plan_mode` 分段 → `prompts/prompt_text/plan_mode/plan_mode.system.md` → 硬编码兜底 |
| `turbo` | 极速模式（对话模式选「极速」时，`agent_factory.py` 以该提示词**整体替换**主系统提示词）。工具集由 Config「系统配置 → 极速模式」动态配置（`turbo.mcp_server_ids`，默认联网搜索/网页抓取/知识库检索三件套，**独立于能力目录的启停**）；用户显式呼唤的技能 / @子智能体 / 插件可临时入场（`turbo.manual_invoke_enable`）；迭代上限可配（`turbo.max_iters`，默认 4）。单一真源为 `prompt_version_service.render_turbo_system_prompt()` | DB `turbo` 激活版本 → `prompts/prompt_text/turbo/turbo.system.md` → 硬编码兜底 |

子智能体不走版本池整版拼装：`prompt_runtime.py::build_subagent_system_prompt()` 以用户自定义 `system_prompt` 为核心，复用激活版本（或文件）的 `20_tools_policy` / `65_citations` / `60_format` 分段组装，详见 [对话与智能体编排](chat.md)。

## 提示词广场（prompt_hub）

提示词广场是面向最终用户的模板库，存于 `ContentBlock(id="prompt_hub")`：

- **前台读取**：`GET /v1/content/docs`（无需鉴权）返回 `prompt_hub` 列表，前端 `src/frontend/src/components/chat/PromptHubPanel.tsx` 在输入区展示、一键填入。
- **后台维护**：`PUT /v1/content/docs/prompt_hub`（`ADMIN_TOKEN`），编辑 UI 为 `src/frontend/src/components/admin/PromptHubEditor.tsx`。

## 跨环境迁移

提示词只存数据库、不随代码发布，跨环境（开发 → 测试 → 生产）迁移依赖快照：

### HTTP 接口（api/routes/v1/content.py）

| 端点 | 说明 |
|---|---|
| `GET /v1/content/prompts/export` | 导出 `prompt_versions` + `prompt_hub` 两个内容块为快照 JSON（与 `page_config` 解耦，不夹带品牌字段） |
| `POST /v1/content/prompts/import?overwrite=true` | 导入快照；**导入后自动失效** `prompt_version_service` 与 `prompt_runtime` 缓存，无需重启后端 |

两端点接受 `ADMIN_TOKEN` 或 `CONFIG_TOKEN`。快照 schema 经 `PROMPT_BLOCK_MAP` 校验，docs 快照与 prompts 快照不能从错误的端点互导。

### 脚本（src/backend/scripts/）

```bash
# 导出（走运行中的后端 API；也支持 --database-url 直连库）
python scripts/export_content.py --api-url http://localhost:3000/api --only prompts
# → scripts/exported/prompts_snapshot_<ts>.json

# 导入到目标环境（用目标机 .env 的 ADMIN_TOKEN）
python scripts/import_content.py --api-url http://<HOST>/api --prompts prompts_snapshot_<ts>.json
# 支持 --no-overwrite / --dry-run
```

离线生产环境（镜像包交付，DB 卷持久化）同样适用：快照文件随镜像包拷入，在 backend 容器内 `curl -X POST .../v1/content/prompts/import` 导入即可，无需重启。换品牌环境迁移时，需人工通读快照逐处改写品牌相关话术后再导入（不要机械查找替换）。

## 相关源码

| 主题 | 路径 |
|---|---|
| 运行时装配 + 缓存 | `src/backend/prompts/prompt_runtime.py` |
| Provider（filesystem/inline/minimal） | `src/backend/prompts/provider.py` |
| 配置（provider/parts/PROMPT_DIR） | `src/backend/prompts/prompt_config.py`，`prompts/config/default.json` |
| 版本池服务 | `src/backend/core/services/prompt_version_service.py` |
| 管理台路由 | `src/backend/api/routes/v1/admin_prompts.py` |
| 迁移接口（export/import） | `src/backend/api/routes/v1/content.py`，`core/content/content_blocks.py` |
| 迁移脚本 | `src/backend/scripts/export_content.py`，`scripts/import_content.py` |
| 系统提示词兜底文件 | `src/backend/prompts/prompt_text/default/system/` |
| 场景提示词兜底 | `src/backend/prompts/prompt_text/{code_exec,distillation,plan_mode,turbo}/` |
| 动态段 | `src/backend/prompts/kb_lite_section.py`，`prompts/project_section.py` |
| 提示词广场前端 | `src/frontend/src/components/chat/PromptHubPanel.tsx`，`components/admin/PromptHubEditor.tsx` |
