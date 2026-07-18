# 提示词系统

> 最后更新：2026-06-11

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
| 模板缓存 `_prompt_cache` | 300s | key 含 provider、parts、工具名集合、MCP keys、DB 版本号、激活版本 `(id, updated_at)`、项目签名等；`{now}` 用占位符存储，渲染时替换为**仅到天**的日期——系统提示词全天逐字节稳定，最大化 LLM 前缀缓存命中 |
| DB parts 预载 `_db_parts_preloaded` | 启动时 `warmup_prompt_cache()` 预载，写后重载 | 首个请求不查库 |
| DB 版本号 `_db_version_cache` | 30s | `MAX(admin_prompt_parts.updated_at)` 作为 cache-busting 版本串 |

任何提示词写操作（管理台编辑、版本激活、快照导入、能力开关变更）都会调 `invalidate_prompt_cache()` 级联清空并立即重热。

## 提示词版本池（prompt_versions）

版本池把多套提示词存进单行 `ContentBlock(id="prompt_versions")`，payload 结构 `{active: {kind: version_id}, versions: [...]}`，服务层为 `core/services/prompt_version_service.py`：

- **四类 kind**（`VALID_KINDS`）：`system`（主智能体）、`code_exec`（代码执行能力段）、`distillation`（技能蒸馏）、`plan_mode`（计划模式）。
- 每个版本含 `(kind, id, name, description, parts[])`，part 即 `{part_id, display_name, content, sort_order, is_enabled}`。
- **API**：`list_versions / get_version / upsert_version（支持 from_id 克隆）/ delete_version（激活中禁删）/ activate_version`；激活后立即失效运行时缓存。
- **Seed**：`seed_from_filesystem()` 首次冷启动把文件系统 markdown 读成默认版本；内置两个一次性迁移——`system/v4 → system/default` 改名、从各 system 版本抽出 `system/90_plan_mode` 生成 `plan_mode/default`。
- 启动时还会幂等补种两个动态段到激活 system 版本：`system/05_system_reminder_convention`（教模型处理 `<system-reminder>` 带外信号）与项目模式段（`prompt_runtime.py::ensure_*_seeded`）。

### Config 管理台

管理入口在 Config 管理台「提示词管理」，对应路由 `api/routes/v1/admin_prompts.py`（`CONFIG_TOKEN` 鉴权）：

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
| 场景提示词兜底 | `src/backend/prompts/prompt_text/{code_exec,distillation,plan_mode}/` |
| 动态段 | `src/backend/prompts/kb_lite_section.py`，`prompts/project_section.py` |
| 提示词广场前端 | `src/frontend/src/components/chat/PromptHubPanel.tsx`，`components/admin/PromptHubEditor.tsx` |
