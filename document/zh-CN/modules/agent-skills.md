# 技能系统（Agent Skills）

> 最后更新：2026-06-11

技能（Agent Skill）是 HugAgentOS 中"教会模型一套作业流程"的标准载体：一个技能 = 一个目录，核心是一份带 frontmatter 的 `SKILL.md` 作业手册，外加任意数量的脚本、模板与参考资料。技能遵循**渐进式披露**（progressive disclosure）原则——系统提示词里只出现技能的名称、描述和目录路径，模型判断需要时才读取完整手册并按手册指挥沙箱执行脚本。

与 [MCP 工具](mcp-tools.md)的分工：MCP 是"程序化的原子能力"（一次调用一个函数），技能是"知识化的工作流"（教模型组合 bash、文件工具和 MCP 完成复杂任务）。办公全家桶（Word/Excel/PPT/PDF）正是从 MCP 形态迁移为技能形态的典型——各技能自带 CLI 引擎在沙箱内执行。

## 技能的解剖

```
<skill-id>/
├── SKILL.md          # 必需：frontmatter（name/description/version/tags）+ 正文作业手册
├── scripts/…         # 可选：可执行脚本（.py/.js/.sh/.r）
├── reference/…       # 可选：参考资料、模板、字体等任意文件
└── _scripts.json     # 可选：脚本白名单声明（缺省时按扩展名自动探测）
```

解析与数据结构在 `core/agent_skills/registry.py`：`AgentSkillMetadata`（轻量元数据，列表场景用）与 `AgentSkillSpec`（含完整指令，执行场景用）。`_scripts.json` 缺失时 `loader._auto_detect_scripts` 按扩展名自动生成白名单，用户无需手写。

## 多源加载与优先级

`core/agent_skills/loader.py::MultiSourceSkillLoader` 通过后端抽象层（`backends/`：filesystem / database / composite）从多个来源加载技能，同 ID 冲突时高优先级覆盖（`core/agent_skills/config.py`）：

| 来源 | 目录 | 优先级 | 说明 |
|---|---|---|---|
| built-in | `src/backend/skill_bundles/default/` | 0 | 随仓库内置，始终启用 |
| user | `~/.hugagent/skills/`（`HUGAGENT_USER_SKILLS_DIR`） | 50 | 文件系统用户技能 |
| admin | `/app/storage/admin_skills/`（`HUGAGENT_ADMIN_SKILLS_DIR`） | 75 | 管理台管理（DB 存储 + 物化） |
| project | `.hugagent/skills/`（`HUGAGENT_PROJECT_SKILLS_DIR`） | 100 | 项目级覆盖 |

每个来源都可用 `HUGAGENT_DISABLE_{ADMIN,USER,PROJECT}_SKILLS=1` 单独禁用。

## skill_bundles 分层：default 与 marketplace

`src/backend/skill_bundles/` 下分两层，加载语义完全不同：

- **`default/` — 5 个内置技能**（always-on，内置加载器 `glob("*/SKILL.md")` 单层扫描命中）：

  | 技能 | 用途 |
  |---|---|
  | `capability-guide-brief` | 能力清单速答（"你能做什么"类问题） |
  | `word-editing` | Word 文档生成/编辑/套模板（word-cli） |
  | `excel-editing` | Excel 工作簿生成/公式建模（excel-cli） |
  | `ppt-design` | PPT 设计与生成（.pptx 产物） |
  | `pdf-editing` | PDF 生成/合并/拆分/填表 |

- **`marketplace/` — 48 个可安装技能包**（安装制）：每个目录含原始 SKILL.md + 引用文件 + 一份 `marketplace.json` 清单。因为它们位于两层深的 `marketplace/<slug>/SKILL.md`，**不会**被内置加载器当作 default 技能自动加载——安装前不出现在 catalog、不注册给智能体，只有显式安装后才落库生效。

  其中 10 个行业/品牌技能（经济指标查询、企业画像查询、产业链结构分析等，硬依赖 [EE 行业 MCP](mcp-tools.md)）属**商业版 EE**，社区版派生树通过 `ce/manifest.yaml` 剔除。

## 注入机制：技能如何进入提示词

技能注册发生在 `core/llm/agent_factory.py` 构建智能体时，按 [catalog](catalog.md) 的 `skills` 段与用户/子智能体配置决定启用集合（私有技能会按 `owner_user_id` 过滤防越权），然后逐个调用 AgentScope 的 `toolkit.register_agent_skill(skill_dir)`。AgentScope 会在系统提示词里生成技能清单，每项含名称、描述和 `{dir}` 目录路径。

这里有一个关键的路径重定向（`loader._repoint_skill_dir_to_sandbox`）：注册时传入的是**后端物理路径**（内置技能在源码树、DB 技能物化在 `/app/storage/sandbox_skills/<id>`），但模型实际执行脚本的地方是**沙箱**，技能在沙箱里的统一路径是 `/workspace/skills/<id>`。因此注册后立即把提示词可见的 `dir` 改写为沙箱路径——否则模型会拿后端路径去调 bash，被路径校验拒绝。

模型读取 SKILL.md 走受限的 `view_text_file` 工具（`core/llm/tools/skill_tool.py::register_sandboxed_view_text_file`，只允许读技能目录内文件，沙箱路径自动映射回后端文件）。读取 SKILL.md 时系统会：

1. 把正文里的 `{baseDir}` 占位符替换为实际目录；
2. 追加一段 Runtime Hint，告诉模型技能文件已就位于 `/workspace/skills/<id>/`、如何用 `bash` 执行脚本、如何用 `sandbox_put_artifact` / `sandbox_get_artifact` 交换输入输出文件。

执行链路示意：

```
用户请求 ──▶ 系统提示词（技能名称+描述+/workspace/skills/<id>）
              │  模型判断需要该技能
              ▼
       view_text_file(SKILL.md)        ← {baseDir} 替换 + Runtime Hint
              │  按手册拼命令
              ▼
       bash("cd /workspace/skills/<id> && python scripts/foo.py …")
              │  在沙箱执行（技能目录只读挂载）
              ▼
       sandbox_get_artifact(产物路径) → 用户可下载
```

### 技能文件如何到达沙箱

所有技能（内置 + DB/管理员导入）通过**单一只读 host bind mount** 暴露进沙箱（详见[沙箱模块](sandbox.md)）：

- 统一宿主目录由 `core/agent_skills/config.py::get_sandbox_skills_dir()` 决定（默认 `$STORAGE_PATH/sandbox_skills`，可用 `SANDBOX_SKILLS_DIR` 覆盖）；
- 后端启动时 `sync_builtin_skills_to_sandbox_dir()` 把内置技能拷入该目录（幂等覆盖，重启即同步编辑）；
- DB 技能按需物化到同一目录（`loader._materialize_skill_files`）；
- 远端 cube 沙箱无 host mount，改为运行时把命中 `/workspace/skills` 的技能文件推送进沙箱（`CUBE_SKILL_PREPUSH` 系列配置）。

## 技能市场（Skill Marketplace）

技能市场是"预置 + 社区"双来源的可安装技能库，核心服务在 `core/services/marketplace_service.py`。

### 浏览与安装

| 接口 | 说明 |
|---|---|
| `GET /v1/marketplace/skills` | 市场列表（预置清单 + 已通过审核的社区技能，标注当前用户是否已装） |
| `GET /v1/marketplace/categories` | 8 个固定分类：写作助手 / 文档处理 / 数据分析 / 政策产业 / 营销创意 / 法务合规 / 办公效率 / 研发效率 |
| `GET /v1/marketplace/skills/{slug}` | 详情（SKILL.md 预览、required_secrets 声明） |
| `POST /v1/marketplace/install` | 用户安装 → **私有技能** |
| `POST /v1/admin/marketplace/install` | 管理员安装 → **全局技能** |

**安装 = 创建一条 `AdminSkill` 记录**，完全复用既有管理员技能机制：

- 管理员安装：`owner_user_id` 为空（全员可用），技能 id = 清单 `entry_name`；
- 用户安装：`owner_user_id` = 当前用户，技能 id 追加用户指纹后缀（`compute_install_id`）保证全局唯一——多个用户可各自安装同一技能、各带各的凭据。

### 凭据机制：required_secrets → secrets.json

`marketplace.json` 可声明 `required_secrets`（如第三方搜索/绘图 API 的 Key）。安装时由前端收集用户填写的凭据，`_inject_secrets` 把它们写入**该安装实例**技能目录下的 `secrets.json`，并在 SKILL.md 末尾追加一段「凭据配置」说明，供脚本运行时读取。市场目录本身**不存任何密钥**。

### 社区上架：提交 → 审核 → 上架/下架

用户可把自己的私有技能申请上架（`marketplace_submissions` 表）：

```
用户  POST /v1/marketplace/submissions      ← 对源技能做内容快照（与后续编辑解耦），
      GET  /v1/marketplace/submissions         注入的凭据段被剥离、required_secrets
      DELETE /v1/marketplace/submissions/{id}   以哨兵文件 _required_secrets.json 随快照保存
                    │
                    ▼
管理员 GET  /v1/admin/marketplace/submissions          （/admin 审核台）
      POST /v1/admin/marketplace/submissions/{id}/approve  → source=community 上架，全员可装
      POST /v1/admin/marketplace/submissions/{id}/reject   → 驳回；驳回已通过的申请即下架
```

审核时管理员可修正分类（仅限 8 个固定分类）。上架快照是**注入凭据前**的形态——安装方各自填自己的 Key。

### 市场可见范围（商业版 EE）

技能市场、插件市场、子智能体市场的条目支持按可见范围投放：默认 `public`（全员可见），管理员可在 `/admin` 各市场管理界面把单个条目设为 `scoped`（指定范围可见），并按**角色 / 团队 / 人员**三类主体勾选白名单——命中任意主体即可见（并集）。可见范围只影响市场的浏览与安装（列表、详情、安装接口同一套过滤），不追溯已安装实例；管理员恒可见。

```
管理员 GET/PUT /v1/admin/marketplace/skills/{slug}/visibility          （技能市场）
      GET/PUT /v1/admin/plugins/market/{slug}/visibility              （插件市场）
      GET/PUT /v1/admin/agent-marketplace/agents/{slug}/visibility    （子智能体市场）
      GET     /v1/admin/visibility/principals    ← 用户/团队/角色简表（选择器数据源）
```

存储：`marketplace_listing_states.visibility`（缺行 = public）+ `marketplace_visibility_grants` 白名单表；用户侧解析收口在 `core/auth/marketplace_visibility.py`（角色含个人直配与经团队获得的部门默认角色）。

## 管理员技能管理

`/admin` 管理台的技能管理对应 `api/routes/v1/admin_skills.py`（前缀 `/v1/admin/skills`），覆盖全生命周期：

- **创建/编辑**：手写 SKILL.md（`POST /`、`PUT /{id}`），或 **zip 上传**整个技能包（`POST /upload`，自动识别目录前缀、二进制文件 base64 入库，上限 200MB）；
- **文件级管理**：`GET/PUT/DELETE /{id}/files/{filename}` 在线编辑技能附属文件；
- **运维**：启停（`/{id}/toggle`）、排序、图标、fork 复制、批量导出/导入（跨环境迁移）；
- **依赖管理**：`POST /{id}/rescan-deps` 用 `core/agent_skills/deps_detector.py` 静态扫描脚本的 pip/apt 依赖，`PUT /{id}/dependencies` 人工修正；聚合后的依赖清单用于沙箱镜像重建（见[沙箱模块](sandbox.md)的管理员依赖重建）。

## 技能蒸馏（Distillation）（商业版 EE）

平台能从历史会话中**自动蒸馏候选技能**：

```
每日 cron（默认 02:30，DISTILL_CRON_EXPRESSION）
  → orchestration/schedulers/distillation_cron_scheduler.py
     扫描前一日活跃会话（Redis 日锁防多实例重复）→ 写 distillation_runs
  → core/llm/skill_distiller.py
     轨迹预检 → LLM 严格 JSON 蒸馏（模型角色 skill_distiller，回退 main_agent）
     → decision = new_skill / patch → 写 admin_skill_drafts（含成本记录）
  → 管理员在 /admin 审核草稿（api/routes/v1/admin_skill_drafts.py）
     approve → 转正为 AdminSkill；reject / delete；可手动触发 trigger-daily-scan
```

阈值、关键词、预算等全部可由 `DISTILL_*` 环境变量覆盖（`core/config/distillation.py`）。蒸馏管线（`skill_distiller.py`、调度器）属**商业版 EE**，社区版派生树剔除；技能草稿审核台随之仅在商业版可见。

## 用户自助技能（能力中心）

普通用户（需管理台授予 `can_add_skill` 权限位）可创建**仅自己可见**的私有技能（`api/routes/v1/me_capabilities.py`）：

| 接口 | 说明 |
|---|---|
| `POST /v1/me/skills/upload` | 上传技能 zip 包（上限 50MB，小于管理员的 200MB） |
| `POST /v1/me/skills` | 手写新建（SKILL.md 内容直接提交） |
| `GET /v1/me/skills/{id}` | 取回用于编辑 |
| `PUT /v1/me/skills/{id}/icon` | 设置图标 |
| `DELETE /v1/me/skills/{id}` | 删除 |

私有技能同样落 `AdminSkill` 表（`owner_user_id` = 本人），运行时 `agent_factory._filter_skill_ids_for_user` 保证不会泄漏给他人。攒出好用的私有技能后即可走上文的社区上架流程贡献到市场。

## 相关源码

| 路径 | 说明 |
|---|---|
| `src/backend/core/agent_skills/registry.py` | SKILL.md 解析、元数据/完整 Spec 数据结构 |
| `src/backend/core/agent_skills/loader.py` | 多源加载、DB 技能物化、沙箱路径重定向 |
| `src/backend/core/agent_skills/config.py` | 加载源优先级、统一沙箱技能目录、内置技能同步 |
| `src/backend/core/agent_skills/selector.py` | 按用户意图 LLM 选技能（渐进式披露） |
| `src/backend/core/agent_skills/deps_detector.py` | 脚本 pip/apt 依赖静态探测 |
| `src/backend/core/agent_skills/backends/` | filesystem / database / composite 加载后端 |
| `src/backend/core/llm/tools/skill_tool.py` | 受限 view_text_file + {baseDir} 替换 + Runtime Hint |
| `src/backend/skill_bundles/default/` | 5 个内置技能 |
| `src/backend/skill_bundles/marketplace/` | 48 个可安装市场技能包 |
| `src/backend/core/services/marketplace_service.py` | 市场列表/安装/凭据注入/上架审核 |
| `src/backend/api/routes/v1/marketplace.py` | 用户侧市场 API（浏览/安装/提交/撤回） |
| `src/backend/api/routes/v1/admin_marketplace.py` | 管理员市场 API（全局安装/审核上架） |
| `src/backend/api/routes/v1/admin_skills.py` | 管理员技能 CRUD / zip / 依赖管理 |
| `src/backend/api/routes/v1/admin_skill_drafts.py` | 蒸馏草稿审核（EE） |
| `src/backend/core/llm/skill_distiller.py` | 技能蒸馏 LLM 管线（EE） |
| `src/backend/orchestration/schedulers/distillation_cron_scheduler.py` | 蒸馏每日调度（EE） |
| `src/backend/api/routes/v1/me_capabilities.py` | 用户自助技能/私有 MCP API |

相关文档：[沙箱执行系统](sandbox.md) · [MCP 工具系统](mcp-tools.md) · [能力目录](catalog.md) · [管理台](admin-console.md) · [版本与许可](../editions/overview.md)
