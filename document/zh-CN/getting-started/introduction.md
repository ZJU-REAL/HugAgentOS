# 产品简介

> 最后更新：2026-10-23 ｜ [English](../../en/getting-started/introduction.md)

HugAgentOS 是一个开源、可自托管的 **Agent Harness**。模型负责思考，Harness 负责模型之外的一切：
任务在哪里运行、断了怎么接上、工具和沙箱怎么用、什么操作需要人批准、经验怎么沉淀。

它的核心是 **Durable Agent**：你提出的问题在云端执行，而不是依赖你的浏览器或电脑。关掉网页、
合上笔记本，任务继续推进；服务器重启或宕机，其他实例从上次保存的进度接着做；不确定是否已经
执行的外部操作不会被自动重做。在此之上，HugAgentOS 提供一个 7×24 小时在线的
**Personal Agent**，并以 **HugAgentOS Cloud** 与 **HugAgentOS Local** 两种形态运行。

平台分为**社区版（CE，开源）**与**商业版（EE）**：社区版让个人完整运行 Harness 与
Personal Agent；商业版补充团队协作、SSO、审计合规、行业数据工具等组织级能力。
详细边界见[版本对比](../editions/overview.md)。

## 运行形态

| 形态 | 运行位置 | 数据与状态 | 适合 | 部署文档 |
|---|---|---|---|---|
| **HugAgentOS Cloud** | 你的服务器或私有云 | PostgreSQL、Redis、S3 兼容存储、OpenSandbox | 7×24 Personal Agent、长任务、IM 渠道、多设备访问 | [Docker Compose](../deployment/docker-compose.md) |
| **HugAgentOS Local** | 你的电脑（Windows / macOS / Linux） | 本地 SQLite、Milvus Lite、本地文件 | 离线、敏感数据、直接处理本地项目 | [桌面离线本机模式](../deployment/desktop-local-mode.md) |
| **本机 + 云端** | 云端为控制面，本机为执行面 | 云端项目存云端，本地项目留本机 | 云端常驻，同时操作本机电脑与项目 | [桌面离线本机模式](../deployment/desktop-local-mode.md) |
| 一键安装 | 开发机 | SQLite、进程内状态、子进程沙箱 | 试用与二次开发 | [无 Docker 一键安装](../deployment/quick-install.md) |

7×24 持久运行需要 HugAgentOS Cloud。一键安装不提供跨进程的租约接管，只适合试用。

## 核心特性总览

| 能力 | 说明 | 版本 |
|---|---|---|
| Durable Run | 每次请求是一条持久化的 Run，后台执行；断线续播、运行中追加指令；租约接管与启动对账；工具副作用记录意图与回执，结果未知不自动重放 | CE |
| Personal Agent | 单一对话入口、并行后台任务、永久云电脑、定时任务与心跳、IM 渠道、操作审批、文件化记忆与 Dreaming、连接本机电脑 | CE（授权治理与跨设备会话管理为 EE） |
| 自主循环 | 目标跨轮存活、做完为止；worker 干活、只读评审员验收，命令可判定的需求由 driver 机检；中断后可续跑 | CE |
| 对话与计划模式 | SSE 流式、ReAct 工具编排、深度思考、计划模式、引用溯源、上下文压缩检查点、会话分叉 | CE |
| 子智能体 | 内置 explorer / worker / reviewer，用户自建子智能体，自动路由或 `@` 提及协作 | CE（组织级智能体库为 EE） |
| MCP 工具与插件 | 内置联网搜索、网页抓取、知识检索、图表、自动化、技能与插件管理等工具；MCP 市场、自建私有 MCP、插件市场、浏览器自动化插件 | CE（数仓查询与行业工具为 EE） |
| Agent Skills | SKILL.md + 脚本，内置技能、技能市场与个人技能 | CE（技能审核与组织治理为 EE） |
| 沙箱执行 | OpenSandbox 会话、环境复用与快照恢复，或本地子进程沙箱；工作流后台作业以受管进程运行，可按租约接管 | CE（CubeSandbox 与团队挂载为 EE） |
| 记忆与个人进化 | L1 画像、L2 做法记忆（Milvus，可选）、L3 关系图谱（Neo4j，可选）；持久 Outbox 写入；进化建议逐条审批 | CE（记忆审计为 EE） |
| 知识库 | 文档分块、向量 + 关键词混合检索、私有知识库、知识库 Wiki | CE（公共库管理与 Dify 对接为 EE） |
| 领域本体治理 | Domain Pack 导入与版本发布、工具执行前规则检查、高风险答案独立评审、演进草案人工审核 | CE |
| 自动化 | 自然语言创建定时任务、Cron 调度、失败重试 | CE |
| 站点与应用托管 | 对话建站并一键发布；应用独立数据库与 MCP 托管 | CE（项目转换后的站点协作为 EE） |
| 数据画布与产物 | Univer 在线表格、图表、报告、Office 文件、网页等产物与产物中心 | CE 个人编辑；多人协同为 EE |
| 项目空间 / 我的空间 | 个人项目、文件夹、收藏、会话分享；桌面端可绑定本地项目目录 | CE（团队文件夹与权限矩阵为 EE） |
| 桌面客户端 | Tauri 2 客户端，自带私有运行时，支持仅本机、仅云端、本机 + 云端三种模式 | CE |

## 技术栈

| 层 | 技术 |
|---|---|
| 智能体运行时 | AgentScope 2.0（ReActAgent）、Model Context Protocol |
| 后端 | Python 3.11、FastAPI、SQLAlchemy 2、Alembic |
| 前端 | React 19、TypeScript、Vite、Zustand、Ant Design |
| 桌面端 | Tauri 2、私有 CPython 3.11 运行时 |
| 数据与状态 | PostgreSQL 15 或 SQLite；Redis 7 或进程内状态 |
| 存储 | S3 兼容存储（云端）、本地文件（桌面与一键安装） |
| 沙箱 | OpenSandbox、本地子进程（CubeSandbox 为 EE） |
| 可选记忆 | Milvus 2.4、Neo4j 5 Community、mem0 |
| 部署 | Docker Compose、桌面安装包、一键安装脚本、Nginx |

## 整体架构

```
 入口：Web · 桌面客户端 · IM 渠道 · API · 定时任务 · 心跳
                         │
                         ▼
 FastAPI（api/）  鉴权 · 版本路由 · 消息先落库再返回
                         │
                         ▼
 Run 层（core/runs · core/chat/runtime）
   持久化 Run · 数据库租约 · 启动对账 · 事件流续播 · steer · 工具副作用回执
                         │
                         ▼
 智能体执行（core/agent_runtime · core/llm）
   ReActAgent · 上下文按步装配与压缩 · 记忆 / 本体 / 进化集成
   外层编排（orchestration/）：计划模式 · 自主循环 · 自动化
                         │
        ┌────────────────┼──────────────────┐
        ▼                ▼                  ▼
  MCP · 技能 · 插件   沙箱 · 后台作业      知识库 · 记忆
        │                │                  │
        └────────────────┴──────────────────┘
                         ▼
 PostgreSQL / SQLite · Redis · S3 / 本地存储 · Milvus · Neo4j
```

Personal Agent（`personal_agent/`）复用同一套 Run、租约与沙箱机制，增加身份、任务队列、
调度、渠道、设备连接和工作区文件。更完整的请求生命周期见[架构总览](../architecture/overview.md)。

## 下一步

- [快速开始](quick-start.md)
- [Personal Agent](../modules/personal-agent.md)
- [对话与智能体编排](../modules/chat.md)
- [自主循环](../modules/autonomous-loop.md)
- [部署指南](../deployment/README.md)

## 相关源码

| 功能 | 路径 |
|---|---|
| FastAPI 应用与路由 | `src/backend/api/app.py`、`src/backend/api/routes/v1/` |
| Run 生命周期、租约与恢复 | `src/backend/core/runs/` |
| 对话 worker、续播与转向 | `src/backend/core/chat/runtime/` |
| 智能体执行入口 | `src/backend/core/agent_runtime/`（`runner.astream_chat_workflow`） |
| 智能体构建 | `src/backend/core/llm/factory/build.py` |
| Harness 契约与执行台账 | `src/backend/core/harness/` |
| 工作流后台作业 | `src/backend/core/jobs/` |
| 计划模式、自主循环、自动化 | `src/backend/orchestration/plans/`、`orchestration/loops/`、`orchestration/automations/` |
| Personal Agent | `src/backend/personal_agent/` |
| 记忆与进化 | `src/backend/core/memory/`、`src/backend/core/evolution/` |
| 领域本体 | `src/backend/core/ontology/` |
| 沙箱 | `src/backend/core/sandbox/` |
| 内置 MCP、技能、插件与智能体 | `src/backend/extensions/` |
| 前端入口 | `src/frontend/src/main.tsx`、`App.tsx` |
| 桌面客户端 | `desktop/src-tauri/` |
