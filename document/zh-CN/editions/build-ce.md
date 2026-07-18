# CE 构建管线
> 最后更新：2026-07-02

社区版（CE）不是独立分支，而是由主仓（EE，唯一开发真源）经 `scripts/build_ce.py` **确定性派生**的子集树，输出到 `dist/ce/`。核心约束是**白名单铁律：EE 专属代码在 CE 树里物理不存在**——不是注释掉、不是 if 关掉，而是文件层面删除。整条管线的唯一输入是 `ce/manifest.yaml`。

## 快速上手

```bash
pip install pyyaml                                  # 生成器唯一三方依赖
python scripts/build_ce.py                          # 生成 + 品牌门禁 + LICENSE 闸门
python scripts/build_ce.py --import-check           # + 在 CE 树上 import api.app 自检
python scripts/build_ce.py --pytest-check           # + pytest --collect-only 自检
python scripts/build_ce.py --frontend-check         # + npm install && build 自检（需网络）
python scripts/build_ce.py --allow-dirty            # 开发期：工作树有未提交改动也生成
python scripts/build_ce.py --allow-placeholder-license  # 开发期：LICENSE 占位文本放行
```

发版生成要求工作树干净（基于已提交状态）；产出目录默认 `dist/ce/`（`--out` 可改）。

## manifest 结构（`ce/manifest.yaml`）

manifest 按处理顺序分为以下几段：

### 1. `exclude` — EE 专属物理排除

glob 模式（相对仓库根），命中即不拷贝。覆盖：

- **后端 EE 模块**：SSO / 团队权限（`core/auth/sso.py`、`team_permissions.py` 等）、云存储（`core/storage/s3.py`、`oss.py`）、持久沙箱 provider（opensandbox / cube 全套）、记忆审计、技能蒸馏、license 验签实现 `core/licensing/_ee_verify.py`、EE service（team / sso_sync / distillation / sandbox_rebuild / security / cube_template_builder）；
- **EE 路由**：`api/routes/v1/admin_*.py`、`config_*.py`、`audit.py`、`auth.py`、`team_files.py`、`service_configs.py`、`data_sources.py`、`db_metadata.py`、`gateway_*.py`；
- **行业 MCP**：`mcp_servers/query_database_mcp/**`、`ai_chain_information_mcp/**`；
- **主仓 alembic 链整体**（`alembic/versions/**`，CE 用 overlay 的独立链，见下文）；
- **10 个行业 / 品牌技能**（`skill_bundles/marketplace/` 下，前 5 个硬依赖 EE 行业 MCP，后 5 个含品牌域文案）；
- **EE 强耦合测试**、`tests/licensing/**`；
- **前端管理台**：`AdminApp.tsx`、`ConfigApp.tsx`、`components/admin/**`、`components/config/**`（lab = 自动化实验室属 CE，保留）；
- **根级 EE 部署物与开源卫生项**：EE Dockerfile / compose 片段、LiteLLM 网关配置（`docker/litellm/**`）、`internal design docs`、`CLAUDE.md`、`.github/**`、内部 `.env` 默认、品牌操作手册 PDF、不可再分发的商业字体（`resources/fonts/**`，overlay 留 README 占位保住 Dockerfile COPY）、内嵌第三方凭据的技能、签发工具 `scripts/license_tool.py`、生成器自身（`ce/**`、`scripts/build_ce.py`）；
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
| `requirements` | `requirements.txt` | 删云存储 / 持久沙箱依赖（boto3 / oss2 / opensandbox）；neo4j / mem0ai 移入可选档 `requirements-mem0.txt` |
| `docker_compose` | `docker-compose.yml` | 删 opensandbox / litellm 服务及 depends_on；`script-runner` 摘掉 profile 转默认启动；整树摘除被排除组件的 env 注入（`OPENSANDBOX_` / `CUBE_` / `S3_` / `OSS_` / `MODEL_GATEWAY_` / `LITELLM_` 前缀） |
| `frontend_lock` | `package-lock.json` + 前端 Dockerfile | 删 lock（与裁剪后的 package.json 必然失同步）、`npm ci` 改 `npm install` |

### 4. `split` — 文件内 user/admin 混合端点的 CE 子集断言

`content.py` / `models.py` / `projects.py` 三个路由文件内同时含用户端点与管理端点，CE 取 overlay 中的 user 子集版本。**build_ce.py 在 overlay 步骤前断言这些文件在 overlay 中存在**——主仓全量版禁止漏进 CE，缺失即 fail。

### 5. `overlay` — CE 专属整文件替换 / 新增

`ce/overlay/` 在 transforms / prunes 之后叠加（内容须自洁，不再经过变换）。当前清单与作用：

| overlay 文件 | 作用 |
|---|---|
| `README.md` / `README_CN.md` / `LICENSE` / `NOTICE` / `CONTRIBUTING.md` / `SECURITY.md` | CE 开源仓门面文件；默认 README 为英文，中文作为语言切换入口保留 |
| `install.sh` | 面向个人无 Docker 模式的公开一键安装脚本 |
| `.env.example` | CE 环境模板（`JX_EDITION=ce`，无内网 IP / 无品牌默认） |
| `resources/fonts/README.md` | 商业字体占位说明（保住 Dockerfile COPY 路径） |
| `src/backend/core/licensing/manager.py` | **CE stub**：`mode()` 恒 `"ce"`、`has()` 恒 False、不限席位，无任何验签实现体 |
| `src/backend/core/auth/permissions_iface.py` | 权限接口层单租户 stub（接缝 C3）：自己的资源恒最高权限，团队权限恒 `none`（存量团队数据不因 stub 放行而对全员可读） |
| `src/backend/core/memory/audit.py` | 记忆审计 no-op stub（同名接口、不落数据） |
| `src/backend/alembic/versions/ce_0001_initial.py` | CE 独立迁移链基线（见下节） |
| `src/backend/api/routes/v1/{content,models,projects}.py` | split 文件的 user 子集版本 |
| `src/backend/mcp_servers/_ports.py` | 8 个通用工具的端口表（EE 行业工具端口标注 reserved） |
| `src/frontend/default.conf.template` | CE 前端 Nginx 模板，移除 `/gateway/**` 反代与 litellm upstream |
| `src/frontend/src/main.tsx` | CE 入口：只挂主应用 / API 文档 / 分享预览，不挂 /admin、/config |
| `src/frontend/src/updates.ts` | CE 版本说明数据 |
| `.claude/skills/hugagent-{backend,frontend}-dev/…` | 项目开发 skill 的 CE 版 SKILL.md 与 references（剔除 admin 面板 / EE 路由注册等商业版段落） |

> 路由注册表 `api/routes/v1/__init__.py` **不需要** overlay 副本：`iter_edition_routers` 对物理缺失的 EE 模块静默跳过，同一份文件两树共用。

### 6. `brand_scan` — 品牌门禁正则文件

`ce/brand_scan.txt` 逐行正则（忽略大小写），命中即 fail。当前拦截：上游品牌词（中英文形态各若干条）、内部字段名、内网 IP 段与测试机 IP、个人邮箱域（@163 / @126 / @qq）、第三方 API key 形态（`ak_[0-9a-f]{16,}`）。

## build_ce.py 步骤流水

```
[1/7] 拷贝     git ls-files（cached + 未跟踪未忽略）为白名单，减 exclude 与默认忽略
[1/7] 改名     按 manifest.renames 执行可选路径迁移（当前配置为空）
[2/7] 变换     manifest.transforms 整树文本替换（二进制免扫；其他产品线品牌字面量告警）
[3/7] 裁剪     manifest.prunes 五个 pruner
[4/7] overlay  先断言 split 文件在 overlay 中存在，再整树叠加（跳过 __pycache__/pyc）
[5/7] 品牌门禁 文本逐行正则 0 命中 + 全量文件「路径」扫描（含二进制资产文件名；
              另有路径专用模式拦商业字体文件本体）；免扫的二进制数随结果上报
[6/7] LICENSE 闸门  overlay LICENSE 仍是占位文本（含 NOTE TO MAINTAINERS 标记）时拒绝生成
[7/7] 自检     --import-check / --pytest-check / --frontend-check（可选）
[8/8] 清残留   自检留下的 __pycache__ / .pytest_cache / node_modules / dist / 再生 lock
```

以 `git ls-files` 为拷贝清单意味着 `.env`、本地数据库等未跟踪 / 已忽略文件**天然不会进入 CE 树**。

## CE 数据库差异

CE 不建 EE 专属表，由 `src/backend/core/db/edition_tables.py` 给出**单一真源**：

- `EE_ONLY_TABLES`（18 张）：`teams`、`team_members`、`team_folders`、`invite_codes`、`roles`、`role_assignments`、`kb_grants`、`audit_logs`、`memory_audit`、`model_pricing`、`data_sources`、`ds_table_meta`、`ds_column_meta`、`ds_golden_sql`、`gateway_virtual_keys`、`sandbox_rebuilds`、`admin_skill_drafts`、`distillation_runs`。
- `ce_create_all(bind)`：在**克隆 MetaData** 上过滤后建表——CE 表里指向 EE 表的跨边界 FK 约束（如 `projects → teams`，方案 D3「列保留、恒 NULL」）若原样下发，PostgreSQL 会因引用表不存在而失败，故在克隆上摘除约束（列保留），原 metadata 与 ORM 映射不受影响。集合里的表名必须真实存在于 metadata（函数内断言），防模型改名后漏更新、悄悄退化成全量建表。

两个建表入口共用该过滤：

1. `core/db/engine.py::init_db` 的 CE 分支（`JX_EDITION=ce` 时走 `ce_create_all`，SQLite 启动兜底）；
2. CE overlay 迁移基线 `ce_0001_initial.py`——CE 走**独立 alembic 链**（不复用主仓 50+ 个历史迁移），基线即「按 `EE_ONLY_TABLES` 过滤后的 create_all」，方言感知（SQLite / PostgreSQL 通吃），与 init_db 同源同滤、两者幂等不冲突。后续 CE schema 演进在 `ce_0001` 链上追加常规迁移。

EE（含 internal / licensed 等全部 license 状态）始终全量建表，行为与历史一致。维护规则：**新增 EE 专属模型时同步把表名加进 `EE_ONLY_TABLES`**。

## 产出验收

一次合格的发布构建须全部通过：

| 闸门 | 标准 | 兑现处 |
|---|---|---|
| 路由零 EE 泄漏 | CE 树物理不含 EE 路由 / 模块；`--import-check` 下 `import api.app` 成功（缺失模块由注册表静默跳过）；`--pytest-check` 收集不报 EE import 错误 | exclude + `iter_edition_routers` |
| 品牌门禁 | 文本 0 命中；全量路径扫描通过；新增二进制资产（免内容扫描）须人工复核 | `brand_scan()` |
| LICENSE 闸门 | overlay LICENSE 非占位文本 | `license_placeholder_check()` |
| split 断言 | 三个 split 文件的 CE 子集在 overlay 中存在 | `main()` overlay 前置检查 |
| 前端可构建 | `--frontend-check`：npm install + vite build 通过 | `frontend_check()` |
| 交付卫生 | 自检残留全部清除 | `cleanup_gate_artifacts()` |

## 日常维护要点

- **新增 EE 路由**：在 `EE_ROUTERS` 注册（见 [后端开发指南](../development/backend.md)）+ `manifest.exclude` 加对应文件 glob（`admin_*.py` / `config_*.py` 已有通配）。
- **新增 EE 表**：`EE_ONLY_TABLES` 加表名。
- **新增 EE 依赖 / compose 服务**：对应 prune 段补 drop 项。
- **新增品牌资产**：确认 brand_scan 能在路径或文本层面拦住；二进制资产是内容扫描盲区，依赖路径模式 + 人工复核。
- 改完跑一次 `python scripts/build_ce.py --allow-dirty --import-check --pytest-check` 验证。

## 相关源码

| 主题 | 路径 |
|---|---|
| 派生清单（唯一输入） | `ce/manifest.yaml` |
| 生成器 | `scripts/build_ce.py` |
| 品牌门禁模式 | `ce/brand_scan.txt` |
| overlay 目录 | `ce/overlay/` |
| CE/EE 建表边界 | `src/backend/core/db/edition_tables.py` |
| 启动建表 CE 分支 | `src/backend/core/db/engine.py::init_db` |
| CE 迁移基线 | `ce/overlay/src/backend/alembic/versions/ce_0001_initial.py` |
| 路由注册表 | `src/backend/api/routes/v1/__init__.py` |

相关阅读：[社区版与商业版总览](overview.md) · [License 机制](license.md)
