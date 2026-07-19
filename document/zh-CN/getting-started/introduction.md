# 产品简介

> 最后更新：2026-07-02

HugAgentOS 是一个企业级 AI Agent 平台：以 ReAct 智能体为内核，把对话、工具调用（MCP）、技能、沙箱代码执行、长期记忆、知识库 RAG、自动化与批量处理整合在一套全容器化（Docker Compose）的系统里。后端基于 FastAPI + AgentScope 2.0，前端基于 React 19 + Vite + Zustand，所有能力通过统一的 catalog 注册表按需启停，开箱即是一个可私有化部署的完整产品，而非框架半成品。

平台分为**社区版（CE，开源）**与**商业版（EE）**两个形态：社区版面向个人与小团队，包含完整的对话、工具、技能、沙箱、三层记忆、自动化、批量、数据画布等能力；商业版在此之上叠加团队协作、SSO、审计合规、行业数据工具、持久沙箱、云存储与完整管理台等组织级能力。详细边界见 [版本对比](../editions/overview.md)。

## 核心特性总览

| 能力 | 说明 | 版本 |
|---|---|---|
| 对话智能体 | SSE 流式对话、AgentScope 2.0 ReActAgent、计划模式（Plan Mode）子智能体、深度思考、`[ref:tool-N]` 引用溯源、会话标题自动摘要 | CE |
| 子智能体 | 用户自建子智能体、@提及协作、路由策略（`ROUTER_STRATEGY`） | CE（版本管理/组织级智能体库为 EE） |
| MCP 工具生态 | 10 个内置 MCP server（CE 8 个通用工具：联网搜索、网页抓取、图表生成、报表导出、批量计划、自动化任务管理、技能管理、知识检索；EE 2 个行业工具：数仓查询、产业链信息），独立 `mcp` 容器以 streamable-http 提供（`http://mcp:9100-9108/mcp/`、`http://mcp:9112/mcp/`）；用户可自助接入远程 HTTP/SSE MCP | CE（产业链/企业画像/数仓等行业工具为 EE） |
| 技能系统 | Agent Skills（SKILL.md + 脚本）：内置技能包、管理台上传、技能市场浏览安装、技能蒸馏 | CE（技能审核/组织治理为 EE） |
| 沙箱执行 | `bash` / `sandbox_put_artifact` / `sandbox_get_artifact` 工具，三种 provider 可切换：script_runner（轻量内置）、OpenSandbox（持久会话 + Jupyter 上下文 + 快照）、CubeSandbox（E2B 兼容 MicroVM） | CE 轻量沙箱；持久沙箱（会话保持/快照）为 EE |
| 记忆系统 | mem0 三层记忆：L1 个人画像、L2 向量记忆（Milvus）、L3 知识图谱（Neo4j），跨会话注入与后台抽取 | CE（记忆审计为 EE） |
| 知识库 RAG | 文档上传、分块、向量 + 关键词混合检索、私有知识库；EE 可追加公共库与 Dify 外部知识库 | CE（公共库与 Dify 对接为 EE） |
| 自动化 | 定时任务 / Cron 调度 / Prompt 与计划自动化 / 失败重试（`orchestration/schedulers/`） | CE |
| 批量执行 | Excel / Word / 列表模板占位符替换，批量计划生成与执行（batch_runner MCP） | CE（团队限额计费为 EE） |
| 数据画布 | 基于 Univer 的在线表格编辑 | CE 个人编辑；多人实时协同为 EE |
| 项目空间 / 我的空间 | 个人项目（文件容量配额）、个人网盘（myspace）、收藏、会话分享 | CE（团队文件夹/权限矩阵为 EE） |
| 管理台 | `/admin` 内容管理（技能、能力中心、版本说明）+ `/config` 系统配置台（提示词版本池、模型、用户/团队、计费、审计、安全） | CE 基础管理；完整管理台（用户/团队/计费/审计/安全）为 EE |

## CE 与 EE

一句话区分：**社区版让一个人把平台用到极致，商业版让一个组织规模化地用起来**——团队协作、SSO、RBAC、审计合规、行业数据工具、持久沙箱、云存储、white-label 属于商业版。完整功能矩阵与授权方式见 [版本对比](../editions/overview.md) 与 [License 机制](../editions/license.md)。

## 技术栈

| 层 | 技术 | 备注 |
|---|---|---|
| 后端 | FastAPI + Uvicorn（Python 3.11） | `src/backend/api/app.py`，统一响应 envelope |
| 智能体框架 | AgentScope 2.0（`agentscope==2.0.0`） | ReActAgent + 工具注册，`core/llm/agent_factory.py` |
| 前端 | React 19 + Vite 7 + Zustand 5 + Ant Design 6 | `src/frontend/`，nginx 容器托管并反代 `/api` |
| 数据库 | PostgreSQL 15（生产）/ SQLite（本地调试兜底） | SQLAlchemy 2 + Alembic 迁移 |
| 缓存 / 会话 | Redis 7 | 会话存储、流式 follower（Redis Streams） |
| 向量库 | Milvus 2.4（mem0 profile） | L2 向量记忆与自建知识库检索 |
| 图数据库 | Neo4j 5 Community（mem0 profile，可选） | L3 知识图谱记忆 |
| 沙箱 | script_runner sidecar / 阿里 OpenSandbox / 腾讯 CubeSandbox | `core/sandbox/` provider 协议，env 切换 |
| 部署 | Docker Compose（profiles：`script_runner` / `opensandbox` / `mem0`） | 全部服务容器化，无本地 dev server |

## 整体架构

```
                        ┌──────────────────────────────────────────────┐
 Browser ──► Nginx ────►│  FastAPI backend (src/backend/api/app.py)    │
        (frontend 容器,  │   api/routes/v1/* · 50+ 路由 · 统一 envelope  │
         /api 反向代理)  └───────────────────┬──────────────────────────┘
                                            │
                     ┌──────────────────────┼─────────────────────────┐
                     ▼                      ▼                         ▼
        orchestration/workflow.py     core/services/*          core/auth/*
        （SSE 流式编排：text /        （业务服务层）           （local / mock /
         tool_call / tool_result                                remote+SSO）
         / meta / done）
                     │
       ┌─────────────┼───────────────────┬─────────────────────┐
       ▼             ▼                   ▼                     ▼
 core/llm/      orchestration/     orchestration/        core/memory/ (svc)
 agent_factory  strategy.py        citations.py          + memory_integration
 （AgentScope    （路由策略）       （引用解析）           （mem0 检索/保存）
  2.0 ReAct）                                                  │
       │                                                       ▼
       ├──► core/llm/mcp_manager ──► mcp 容器                Milvus / Neo4j
       │      （10 个 MCP server, http://mcp:9100-9108/mcp/ + :9112）  （mem0 profile）
       ├──► core/sandbox/* ──► script-runner / OpenSandbox / CubeSandbox
       └──► PostgreSQL · Redis · 存储 (local / S3 / OSS)
```

请求主链路：浏览器 → frontend 容器内 nginx（`/api` 反代）→ FastAPI → `orchestration/workflow.py` 编排流式输出 → `core/llm/agent_factory.py` 构建 ReActAgent → MCP 工具 / 沙箱 / 记忆 → SSE 事件（`text` / `tool_call` / `tool_result` / `meta` / `done`）回推前端。

## 下一步

- [10 分钟快速开始](quick-start.md)
- [Docker Compose 完整部署](../deployment/docker-compose.md)
- [环境变量参考](../deployment/environment-variables.md)
- [架构总览](../architecture/overview.md)

## 相关源码

| 功能 | 文件 |
|---|---|
| FastAPI 应用与路由注册 | `src/backend/api/app.py`、`src/backend/api/routes/v1/` |
| 流式编排（SSE） | `src/backend/orchestration/workflow.py`、`orchestration/streaming.py` |
| 智能体构建 | `src/backend/core/llm/agent_factory.py` |
| MCP server 与端口映射 | `src/backend/mcp_servers/`、`src/backend/mcp_servers/_ports.py`、`src/backend/core/config/mcp_config.py` |
| 能力注册表 | `src/backend/core/config/catalog.json`、`core/config/catalog.py` |
| 沙箱 provider | `src/backend/core/sandbox/`、`src/backend/core/config/settings.py::SandboxSettings` |
| 记忆系统 | `src/backend/core/memory/`（service.py / pipeline.py）、`src/backend/orchestration/memory_integration.py` |
| 自动化调度 | `src/backend/orchestration/schedulers/automation_scheduler.py` |
| 前端入口（App / Admin / Config） | `src/frontend/src/main.tsx`、`App.tsx`、`AdminApp.tsx`、`ConfigApp.tsx` |
| 版本与 License 设置 | `src/backend/core/config/settings.py::EditionSettings / LicenseSettings` |
