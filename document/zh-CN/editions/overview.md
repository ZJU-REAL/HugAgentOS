# 社区版与商业版总览
> 最后更新：2026-07-22

HugAgentOS 采用 **open-core（开放内核）** 模式发行，对标 Dify / FastGPT 等开源 Agent 平台的通行做法：

- **社区版（CE，Community Edition）**：开源免费。一个人、一台机器即可完整自洽运行——完整对话、知识库 RAG、子智能体、通用工具、自动化、批量执行、数据画布与三层个人记忆全部包含。
- **商业版（EE，Enterprise Edition）**：在社区版之上叠加**组织规模化**能力——团队协同、SSO、审计合规、计费、行业数据工具、管理台与 white-label，按年订阅授权（离线 License 文件）。

划分原则一句话：**个人自洽的能力归社区版，组织规模化的能力归商业版**。

## 仓库形态

本仓库（主仓）即商业版（EE）的唯一开发真源，默认 `JX_EDITION=ee`（`.env.example`）。社区版**不是独立维护的分支**，而是由主仓经构建管线**确定性派生**出的子集树：

```bash
python scripts/build_ce.py        # 生成 dist/ce/（CE 派生树）
```

派生树中 EE 专属代码**物理不存在**（白名单铁律）。CE 与 EE 统一使用 `HugAgentOS` 展示品牌，并保留 `hugagent` 技术标识；派生细节见 [CE 构建管线](build-ce.md)。

```
主仓（EE，本仓库）
  ├── 商业交付：镜像包 + License 文件（离线授权）
  └── scripts/build_ce.py ──► dist/ce/（CE 派生树，开源发布）
```

## 功能对比矩阵

版本边界与官网定价页对齐。下表为当前代码已落地的实际边界：

| 能力域 | 社区版（CE） | 商业版（EE）新增 |
|---|---|---|
| 智能对话 | ✅ 完整：SSE 流式、ReAct 智能体、计划模式、深度思考、引用溯源 | — |
| 知识库 RAG | ✅ 文档上传、智能分块、向量 + 关键词混合检索、私有知识库 | ➕ 对接外部知识库（Dify）、公共知识库管理台 |
| 子智能体 | ✅ 创建、自动路由、@提及协作（个人） | ➕ 组织级智能体库与管理台 |
| MCP 工具 | ✅ 8 个通用工具：联网搜索、网页抓取、图表生成、报表导出、批量执行、自动化任务管理、技能管理、知识库检索 | ➕ 2 个行业工具：数仓查询（`query_database`）、产业知识中心（`ai_chain_information_mcp`） |
| 能力中心自助 | ✅ 自建私有 MCP（远程 HTTP/SSE）、私有技能（手写 / zip 上传），owner 隔离 | ➕ 组织级权限位治理、技能审核与蒸馏 |
| 个人 API-Key | ✅ 自建 / 吊销个人 API 密钥，调用原生智能体 API | ➕ 对外模型网关（OpenAI / Anthropic 兼容）与组织按用户授权位统一管控 |
| 记忆系统 | ✅ L1 个人画像 + L2 向量（Milvus，可选）+ L3 图谱（Neo4j，可选） | ➕ 记忆审计（合规留痕，`memory_audit` 表） |
| 自动化 | ✅ 定时任务、Cron 调度、Prompt/计划自动化、失败重试 | — |
| 批量执行 | ✅ Excel/Word/列表模板批量处理 | — |
| 数据画布 | ✅ 在线表格个人编辑（免费 Univer 预设） | ➕ 多人实时协同（含 `@univerjs/preset-sheets-advanced` 商业预设） |
| 代码执行 | ✅ 轻量沙箱（script-runner）+ 超长结果 offload 回读 | ➕ 持久沙箱（OpenSandbox / Cube provider，会话保持、环境复用） |
| 文件存储 | ✅ 本地存储 | ➕ 云存储（S3 / 阿里云 OSS） |
| 认证登录 | ✅ 本地账号注册登录 | ➕ 企业 SSO 单点登录、部门同步、邀请码体系 |
| 个人工作空间 | ✅ 个人文件夹、收藏、会话分享、个人项目 | — |
| 团队协作 | — | ➕ 团队、成员管理、团队文件夹、权限矩阵、会话团队共享 |
| 安全审计 | — | ➕ 操作审计、聊天历史审查、调用日志、安全管理台 |
| 计费与用量 | ✅ 查看自己的 Token 用量 | ➕ 计费报表（汇总、模型定价、CSV 导出）；配额管控（规划中） |
| 内容管理台（/admin） | —（个人能力中心自助替代） | ➕ 技能 / 提示词版本灰度 / MCP / 智能体 / 知识库管理 |
| 系统管理台（/config） | — | ➕ 用户 / 团队 / 邀请 / 安全 / 服务配置 / License 面板 |
| 品牌定制 | ⚠️ 可改品牌（保留署名） | ➕ 完全 white-label |

> 注意 2026-06 边界调整：**自动化、批量执行、数据画布（个人编辑）、记忆 L2 向量 / L3 图谱已由商业版下放社区版**。商业版保留的是其组织协同增量（画布多人协同、团队限额计费等）。

## 版本与 License 的运行时表达

后端用两个环境变量界定形态（`src/backend/core/config/settings.py` 中的 `EditionSettings` / `LicenseSettings`）：

| 形态 | 配置 | 行为 |
|---|---|---|
| 社区版 | `JX_EDITION=ce`（CE 派生树 `.env.example` 默认值） | CE 树物理不含 License 实现；版本探针固定返回 CE 形态与空的 EE 能力集，不执行验签、不限制席位 |
| 商业版 · internal | `JX_EDITION=ee` 且未配置 license 文件且 `JX_LICENSE_REQUIRED=false`（主仓默认） | **内部 / 全托管部署模式：全功能放行**，与历史部署行为完全一致——存量部署升级到含 license 机制的版本后无需任何配置变更 |
| 商业版 · licensed | `JX_EDITION=ee` + 有效 license 文件 | 按 license 中的 entitlement（能力位清单 + 席位 + 有效期）放行 |

每个部署都暴露无鉴权探针 `GET /v1/meta/edition`（`src/backend/api/routes/v1/meta.py`），返回 `edition` / `mode` / 能力位布尔表；前端 `stores/editionStore.ts` 启动时拉取并据此隐藏 EE 入口（如团队页签）。完整状态机与执法机制见 [License 机制](license.md)。

## CE 的获取方式

社区版以**派生树**形式发布：

1. 上游在主仓打发布版本后运行 `scripts/build_ce.py`，生成 `dist/ce/`；
2. 生成过程经过品牌门禁（0 命中才放行）与 import / pytest / 前端构建自检；
3. `dist/ce/` 以独立开源仓库形式对外发布（`ce/overlay/README.md` 即其 README，标注 `generated`，`src/**` 改动经 Issue / Discussion 反馈）。

社区版快速启动（在 CE 树内）：

```bash
cp .env.example .env
docker compose up -d --build  # 前端 :3002 · 后端 :3001
# 启动后登录，在「设置 → 系统管理 → 模型服务」配置模型接入（搜索引擎等在「服务配置」）
# 可选 L2/L3 记忆组件：
COMPOSE_PROFILES=mem0 docker compose up -d
```

## 升级路径

- **CE → EE**：商业版以**镜像包 + License 文件**交付（适配政务内网离线环境）。CE 表集合是 EE 的真子集：20 张 EE 表、其外键及共享资源上的商业作用域列均不注册；升级时由交付迁移补齐组织结构。
  > ⚠️ 注意：CE 走独立迁移链（基线 `ce_0001`，见 [CE 构建管线](build-ce.md#ce-数据库差异)），与 EE 的主仓迁移链不同；CE 存量库切换 EE 镜像时的 alembic 版本对接由交付实施完成（当前无自动转换工具，属规划中）。
- **EE internal → EE licensed**：私有化交付时配置 `LICENSE_KEY_PATH` 与 `JX_LICENSE_REQUIRED=true`，在 `/config` 管理台 License 面板上传 `.lic` 文件即时激活，无需重启。
- **续期 / 扩容**：上传新 license 文件热替换（同上），到期后有宽限期缓冲（默认 14 天）。

## 相关源码

| 主题 | 路径 |
|---|---|
| 版本 / License 配置 | `src/backend/core/config/settings.py`（`EditionSettings` / `LicenseSettings`） |
| License 状态机 | `src/backend/edition_ee/licensing/manager.py` |
| EE 能力位枚举 | `src/backend/edition_ee/licensing/features.py` |
| 路由注册表（CE/EE 两表） | `src/backend/api/routes/v1/__init__.py` |
| 版本探针 | `src/backend/api/routes/v1/meta.py` |
| 前端 edition 门控 | `src/frontend/src/stores/editionStore.ts` |
| CE 派生清单 | `ce/manifest.yaml` |
| CE 生成器 | `scripts/build_ce.py` |

下一步：[License 机制（商业版）](license.md) · [CE 构建管线](build-ce.md) · [后端开发指南](../development/backend.md)
