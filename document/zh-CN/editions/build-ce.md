# CE 构建管线

> 最后更新：2026-07-23

社区版（CE）不是独立分支，而是由主仓（EE，唯一开发真源）经 `scripts/build_ce.py` **确定性派生**的子集树，输出到 `dist/ce/`。核心约束是**白名单铁律：EE 专属代码在 CE 树里物理不存在**——不是注释掉、不是 if 关掉，而是文件层面删除。整条管线的唯一输入是 `ce/manifest.yaml`。

## 快速上手

```bash
pip install pyyaml                                  # 生成器唯一三方依赖
python scripts/build_ce.py                          # 生成 + 品牌门禁 + LICENSE 闸门
python scripts/build_ce.py --import-check           # + 在 CE 树上 import api.app 自检
python scripts/build_ce.py --pytest-check           # + 可执行 CE 发布回归测试
python scripts/build_ce.py --frontend-check         # + npm install && build 自检（需网络）
python scripts/build_ce.py --allow-dirty            # 开发期：工作树有未提交改动也生成
python scripts/build_ce.py --allow-placeholder-license  # 开发期：LICENSE 占位文本放行
```

发版生成要求工作树干净（基于已提交状态）；产出目录默认 `dist/ce/`（`--out` 可改）。

## manifest 结构（`ce/manifest.yaml`）

manifest 按处理顺序分为以下几段：

### 1. `exclude` — EE 专属物理排除

glob 模式（相对仓库根），命中即不拷贝。覆盖：

- **后端 EE 模块**：完整的 `edition_ee/**` 实现根（团队/RBAC、SSO、License 验签与闸门、EE ORM、Dify 集成），以及云存储（`core/storage/s3.py`、`oss.py`）、持久沙箱 provider、记忆审计、技能蒸馏等 EE 服务；
- **EE 路由**：`api/routes/v1/admin_*.py`、`config_*.py`、`audit.py`、`auth.py`、`team_files.py`、`service_configs.py`、`data_sources.py`、`db_metadata.py`、`gateway_*.py`；
- **行业 MCP**：`mcp_servers/query_database_mcp/**`、`ai_chain_information_mcp/**`；
- **主仓 alembic 链整体**（`alembic/versions/**`，CE 用 overlay 的独立链，见下文）；
- **10 个行业 / 品牌技能**（`skill_bundles/marketplace/` 下，前 5 个硬依赖 EE 行业 MCP，后 5 个含品牌域文案）；
- **EE 强耦合测试**、`tests/licensing/**`；
- **前端管理台**：`AdminApp.tsx`、`ConfigApp.tsx`、`components/admin/**`、`components/config/**`（lab = 自动化实验室属 CE，保留）；
- **根级 EE 部署物与开源卫生项**：EE Dockerfile / compose 片段、LiteLLM 网关配置（`docker/litellm/**`）、`internal design docs`、`CLAUDE.md`、`.github/**`、内部 `.env` 默认、品牌操作手册 PDF、内置商业字体资产、内嵌第三方凭据的技能、签发工具 `scripts/license_tool.py`、生成器自身（`ce/**`、`scripts/build_ce.py`）；
- **项目开发 skill 的 EE 专属模板**：`.claude/skills/*/templates/admin_route.py`、`admin-editor.tsx`（admin 路由 / 内容台在 CE 物理不存在）。

### 1.5 `renames` — 可选路径改名

transforms 只改文件内容不改路径，因此本步骤为确有需要的路径迁移预留。CE 与 EE 目前统一使用 HugAgentOS 品牌和 `hugagent` 技术标识，`renames` 为空，`.claude/skills/hugagent-*-dev` 等路径原样保留。

### 2. `transforms` — 品牌一致性与开源卫生文本变换

按声明顺序整树应用于文本文件。CE 产品名为 `HugAgentOS`（`product_name` 字段），`hugagent` 容器名、环境变量、CLI 等技术标识原样保留；另一产品线的 HugAgentOS 字面量会统一为 HugAgentOS，历史展示名 `HugAgentOS` 则通过带负向前瞻的正则升级为 `HugAgentOS`，避免重复追加 `OS`。

> 生成器对 `src/**` 源码中仍含其他产品线品牌字面量的文件会单独点名告警（`_GENERIC_BRAND_TOKENS` 检查）——这类硬编码应收敛到 `settings.branding` / DB seed，而非长期依赖派生变换。

### 3. `prunes` — 结构化裁剪

无法用纯文本变换表达的内容手术，实现内置在 `build_ce.py` 的 `PRUNERS` 表：

| pruner | 目标 | 动作 |
|---|---|---|
| `catalog_json` | `core/config/catalog.json` | 去掉 EE MCP 种子（`database_query`、`query_database`、`ai_chain_information_mcp`） |
| `package_json` | `src/frontend/package.json` | 改名 `hugagent-ui`；删商业 License 预设 `@univerjs/preset-sheets-advanced` 与死依赖 `pptxgenjs`；固定 `@univerjs/icons=1.1.1`，避免无主仓 lockfile 时与 Univer 0.19 发生导出契约不兼容 |
| `requirements` | `requirements.txt` | 删云存储 / 持久沙箱依赖（boto3 / oss2 / opensandbox）；neo4j / mem0ai 移入独立档 `requirements-mem0.txt`（无 Docker 一键安装器会默认安装该档） |
| `docker_compose` | `docker-compose.yml` | 删 opensandbox / litellm 服务及 depends_on；`script-runner` 摘掉 profile 转默认启动；整树摘除被排除组件的 env 注入（`OPENSANDBOX_` / `CUBE_` / `S3_` / `OSS_` / `MODEL_GATEWAY_` / `LITELLM_` 前缀）；把后端版本、认证与 SSO 默认值固定为 CE 本地会话模式，前端构建默认固定为 CE |
| `frontend_lock` | `package-lock.json` + 前端 Dockerfile | 删 lock（与裁剪后的 package.json 必然失同步）、`npm ci` 改 `npm install` |
| `repository_resources` | 内置商业字体的构建与源码引用 | 从 CE Dockerfile 删除字体复制/安装段，并清除后端的仓库字体目录回退引用；生成后由禁止产物门禁再次检查 |

### 4. `split` — 文件内 user/admin 混合端点的 CE 子集断言

`manifest.split` 明确列出所有必须由 CE overlay 整文件替换的版本接缝。**build_ce.py 在 overlay 步骤前逐项断言替代文件存在**——源树全量实现禁止漏进 CE，缺失即 fail。

### 5. `overlay` — CE 专属整文件替换 / 新增

`ce/overlay/` 在 transforms / prunes 之后叠加（内容须自洁，不再经过变换）。当前清单与作用：

| overlay 文件 | 作用 |
|---|---|
| `README.md` / `README_CN.md` / `LICENSE` / `NOTICE` / `CONTRIBUTING.md` / `SECURITY.md` | CE 开源仓门面文件；默认 README 为英文，中文作为语言切换入口保留 |
| `install.sh` | 面向个人无 Docker 模式的公开一键安装脚本 |
| `.env.example` | CE 环境模板（`JX_EDITION=ce`，无内网 IP / 无品牌默认） |
| `.hugagent-edition` | 仅在派生后出现的机器可读 `ce` 标识；让发布工具区分派生 CE checkout 与生成器异常缺失的源代码 checkout |
| `.github/workflows/desktop-release.yml` | 公开 CE 桌面发版 workflow，包含 release tag / 版本前置门禁 |
| `src/backend/api/routes/v1/__init__.py` | CE 路由注册表；`EE_ROUTERS` 恒为空 |
| `src/backend/core/auth/permissions_iface.py` | 单租户 owner-only 权限接口；不导出团队权限函数 |
| `src/backend/core/services/artifact_edition.py` | 个人空间 artifact 作用域接口；不暴露团队字段、权限或仓储方法 |
| `src/backend/core/llm/tools/edition_{myspace,myspace_vfs,artifact_recovery}.py` | 个人空间工具、VFS 与恢复接口；组织空间实现不进入 CE |
| `src/backend/core/config/edition_display_names.py` | CE 工具展示名；不包含团队工具名称 |
| `src/backend/core/memory/audit.py` | 记忆审计 no-op stub（同名接口、不落数据） |
| `src/backend/alembic/versions/ce_000{1,2}_*.py` | CE 独立迁移链基线与幂等 Schema 升级（见下节） |
| `src/backend/core/services/edition_startup.py` | CE 启动接缝；为 EE 数据源恢复、蒸馏调度提供 no-op 实现，避免导入已裁除模块 |
| `src/backend/tests/ce_release/` | 可执行 CE 发布回归套件，覆盖登录、私有技能/MCP、数据库升级及启动/Compose 契约 |
| `src/backend/api/routes/v1/{agents,content,kb_models,projects}.py` | 去除管理端点与组织字段后的 CE API 契约 |
| `src/backend/mcp_servers/_ports.py` | 8 个通用工具的端口表（EE 行业工具端口标注 reserved） |
| `src/frontend/default.conf.template` | CE 前端 Nginx 模板，移除 `/gateway/**` 反代与 litellm upstream |
| `src/frontend/src/main.tsx` | CE 入口：只挂主应用 / API 文档 / 分享预览，不挂 /admin、/config |
| `src/frontend/src/updates.ts` | CE 版本说明数据 |
| `.claude/skills/hugagent-{backend,frontend}-dev/…` | 项目开发 skill 的 CE 版 SKILL.md 与 references（剔除 admin 面板 / EE 路由注册等商业版段落） |

> License、Team/RBAC、EE ORM 与 Dify 的实现根均在 `edition_ee/**`，CE 不提供同名实现 stub；派生树内对 `edition_ee` 的 import 探测必须返回不存在。

团队文件仓储、组织 MySpace 工具、VFS 与 artifact 恢复实现分别位于
`edition_ee/db/artifact_repository.py` 和
`edition_ee/services/{myspace_tools,myspace_vfs,artifact_recovery}.py`。
共享模块只保留版本中性的调用接口，CE overlay 提供个人版实现，不保留商业字段或工具名。

### 6. `brand_scan` — 品牌门禁正则文件

`ce/brand_scan.txt` 逐行正则（忽略大小写），命中即 fail。当前拦截：上游品牌词（中英文形态各若干条）、内部字段名、内网 IP 段与测试机 IP、个人邮箱域（@163 / @126 / @qq）、第三方 API key 形态（`ak_[0-9a-f]{16,}`）。

## build_ce.py 步骤流水

```
[1/7] 拷贝     git ls-files --cached 为白名单，减 exclude 与默认忽略
[1/7] 改名     按 manifest.renames 执行可选路径迁移（当前配置为空）
[2/7] 变换     manifest.transforms 整树文本替换（二进制免扫；其他产品线品牌字面量告警）
[3/7] 裁剪     manifest.prunes 五个 pruner
[4/7] overlay  先断言 split 文件在 overlay 中存在，再整树叠加（跳过 __pycache__/pyc）
[4/7] 禁止产物 断言 EE 路径、表名、外键和运行时源码商业符号均为 0 命中；
              测试目录只允许保存“不得出现”的负向契约断言
[4/7] 二进制门禁 PNG/PDF/DOCX 必须匹配经人工/OCR 审阅后的 path + SHA-256 白名单
[5/7] 品牌门禁 文本逐行正则 0 命中 + 全量文件「路径」扫描（含二进制资产文件名；
              另有路径专用模式拦商业字体文件本体）
[6/7] LICENSE 闸门  overlay LICENSE 仍是占位文本（含 NOTE TO MAINTAINERS 标记）时拒绝生成
[7/7] 自检     --import-check / --pytest-check / --frontend-check（可选）
[8/8] 清残留   自检留下的 __pycache__ / .pytest_cache / ce_selfcheck.db / node_modules / dist / 再生 lock
```

以 `git ls-files` 为拷贝清单意味着 `.env`、本地数据库等未跟踪 / 已忽略文件**天然不会进入 CE 树**。

Windows 和 macOS 桌面服务载荷在两类 checkout 中遵守同一边界：源代码 checkout 中，
`desktop/scripts/prepare-bundle.mjs` 仍会找到并运行 `scripts/build_ce.py`；在有意移除生成器的公开 CE 仓中，它要求
`.hugagent-edition` 内容为 `ce`，然后只暂存当前 checkout 的 Git tracked 文件。正式发布会拒绝脏
checkout，因此该 fallback 不能把一个生成器异常缺失的任意仓库静默当成 CE 载荷。边界检查和前端
构建完成后，脚本把派生树压成单个 `server-ce.zip` 再交给平台安装包，避免安装和卸载阶段处理数千个
散文件。

## CE 数据库差异

CE 不注册、也不创建 EE 专属表。商业 ORM 类集中在 `src/backend/edition_ee/db/models/`，该目录在派生树中物理不存在；CE 的模型出口只导出 CE 映射，兼容属性由 CE model extension 提供，不注册商业列或表。

发布契约禁止 20 张表：`chat_session_user_states`、`teams`、`team_members`、`team_folders`、`invite_codes`、`roles`、`role_assignments`、`kb_grants`、`marketplace_visibility_grants`、`audit_logs`、`memory_audit`、`model_pricing`、`data_sources`、`ds_table_meta`、`ds_column_meta`、`ds_golden_sql`、`gateway_virtual_keys`、`sandbox_rebuilds`、`admin_skill_drafts`、`distillation_runs`。import 门禁会同时检查 CE metadata 中没有这些表、没有跨界外键；`projects`、`artifacts`、`user_agents`、`chat_sessions`、`marketplace_listing_states` 与 `sites` 也不得注册相应商业作用域列。

CE 的所有建表和升级入口共用 CE-only metadata：

1. `ce_0001_initial.py` 是不可回写的历史基线；新安装从 CE-only metadata 建表，方言感知（SQLite / PostgreSQL 通吃）。
2. `ce_0002_reconcile_schema.py` 对早期 CE 基线可能缺少的表、列和索引执行幂等补齐。已存在对象保持不变，重复执行安全；不允许通过删除或重建用户表来“追平”结构。
3. `core/db/engine.py::init_db` 的 CE 分支在 SQLite/no-Docker 启动时执行同一个 reconciler，覆盖没有运行 Alembic 的桌面升级路径。
4. Docker/PostgreSQL 先运行 Alembic，再由入口脚本防御性执行一次 `init_db`；这样历史数据库和新数据库最终收敛到相同 CE Schema。

以后每次共享 CE 模型发生结构变化，都必须追加新的 `ce_000N` 迁移，并保持 reconciler 可重复执行；禁止只修改 `ce_0001` 或依赖 `create_all()` 给已有表补列，因为 SQLAlchemy 不会修改已存在表。

EE 始终全量建表。维护规则：**新增 EE 专属模型必须放进 `edition_ee/db/models`，并把表名加入 CE 发布契约**。

## 产出验收

一次合格的发布构建须全部通过：

| 闸门 | 标准 | 兑现处 |
|---|---|---|
| 路由 / Schema 零 EE 泄漏 | CE 树物理不含 `edition_ee`；`EE_ROUTERS` 为空；OpenAPI 无组织路由、字段或文案；metadata 无禁止表、跨界外键与商业作用域列 | `--import-check` |
| 运行时源码零商业符号 | 后端与前端运行时源码不得出现 Team/RBAC 的模型、作用域字段、权限或工具符号；测试目录仅保留负向断言 | `find_forbidden_artifacts()` + CE runtime contract |
| 品牌 / 二进制门禁 | 文本 0 命中、全量路径扫描通过；PNG/PDF/DOCX 必须命中经人工/OCR 审阅的 path + SHA-256 白名单 | `brand_scan()` + `binary_allowlist_check()` |
| LICENSE 闸门 | overlay LICENSE 非占位文本 | `license_placeholder_check()` |
| split 断言 | 每个声明的 CE split 替代文件都在 overlay 中存在 | `main()` overlay 前置检查 |
| CE 核心流程可执行 | `--pytest-check`：实际调用 admin/admin 登录与 ticket 兑换，创建私有技能/MCP，升级旧 SQLite Schema，并检查 CE 启动与 Compose 默认值 | `pytest_check()` + `tests/ce_release/` |
| 前端可构建 | `--frontend-check`：npm install + vite build 通过 | `frontend_check()` |
| 交付卫生 | 自检残留全部清除 | `cleanup_gate_artifacts()` |

## 日常维护要点

- **新增 EE 路由**：在 `EE_ROUTERS` 注册（见 [后端开发指南](../development/backend.md)）+ `manifest.exclude` 加对应文件 glob（`admin_*.py` / `config_*.py` 已有通配）。
- **新增 EE 表**：模型放进 `edition_ee/db/models`，表名加入 `contracts.forbidden_tables`。
- **新增 EE 依赖 / compose 服务**：对应 prune 段补 drop 项。
- **新增 PNG/PDF/DOCX**：先人工检查或 OCR 提取复核内容，再把相对路径与 SHA-256 加入 `ce/binary_allowlist.sha256`；哈希变化必须重新审阅。
- 改完跑一次 `python scripts/build_ce.py --allow-dirty --import-check --pytest-check --frontend-check` 验证；正式发布不得使用 `--allow-dirty`，且未跟踪文件永不作为复制输入。

## 相关源码

| 主题 | 路径 |
|---|---|
| 派生清单（唯一输入） | `ce/manifest.yaml` |
| 生成器 | `scripts/build_ce.py` |
| 品牌门禁模式 | `ce/brand_scan.txt` |
| overlay 目录 | `ce/overlay/` |
| 派生树版本标识 | `ce/overlay/.hugagent-edition` |
| 公开桌面发版 workflow | `ce/overlay/.github/workflows/desktop-release.yml` |
| CE/EE 建表边界 | `src/backend/core/db/edition_tables.py` |
| 幂等 Schema 升级器 | `src/backend/core/db/schema_reconcile.py` |
| 启动建表 CE 分支 | `src/backend/core/db/engine.py::init_db` |
| CE 独立迁移链 | `ce/overlay/src/backend/alembic/versions/ce_000{1,2}_*.py` |
| CE 发布回归套件 | `ce/overlay/src/backend/tests/ce_release/` |
| 路由注册表 | `src/backend/api/routes/v1/__init__.py` |

相关阅读：[社区版与商业版总览](overview.md) · [License 机制](license.md)
