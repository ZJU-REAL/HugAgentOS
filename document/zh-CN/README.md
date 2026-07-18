# HugAgentOS 中文文档

> 最后更新：2026-06-11 ｜ [English](../en/README.md)

HugAgentOS 是一个企业级 AI Agent 平台：FastAPI 后端 + React 前端 + AgentScope 2.0 智能体底座，全栈 Docker 化部署，以 open-core 模式提供**开源社区版（CE）**与**商业版（EE）**。本目录是面向部署者、使用者与二次开发者的完整产品文档。

## 快速上手

| 文档 | 说明 |
|------|------|
| [产品简介](getting-started/introduction.md) | 是什么、核心特性总览、CE/EE 一览、技术栈与整体架构 |
| [快速开始](getting-started/quick-start.md) | 10 分钟用 Docker Compose 跑起来 |

## 部署

先看 [部署指南（选择部署方式）](deployment/README.md)，再按方式查阅：

| 文档 | 说明 |
|------|------|
| [部署指南 · 总览](deployment/README.md) | 各部署方式对比与选型、部署后验证 |
| [无 Docker 一键安装](deployment/quick-install.md) | 个人单机零依赖：一条命令装好，SQLite + 进程内 fakeredis + 子进程 MCP/沙箱 |
| [Docker Compose 部署](deployment/docker-compose.md) | 团队/生产标准形态：全部服务拓扑、profiles、rebuild 流程、数据库迁移 |
| [离线生产部署](deployment/offline-production.md) | 隔离环境：镜像 tarball 打包 / 生产侧加载 / 提示词快照迁移（商业版 EE） |
| [Windows 部署](deployment/windows-deployment.md) | Docker Desktop / WSL2 部署差异、换行符、路径与沙箱限制 |
| [环境变量参考](deployment/environment-variables.md) | 全量环境变量逐组说明（默认值 / 作用 / CE·EE 相关性） |

## 架构

| 文档 | 说明 |
|------|------|
| [总体架构](architecture/overview.md) | 分层架构图、一次对话的完整生命周期、容器拓扑、关键设计决策 |
| [后端架构](architecture/backend.md) | `src/backend/` 全目录详解：api / orchestration / core 15 子模块 / 路由注册表 |
| [前端架构](architecture/frontend.md) | 五入口分发、组件组、Zustand store、hooks、构建链 |
| [数据模型](architecture/data-model.md) | 44 张表分组速览、alembic 双迁移链、CE/EE 表边界 |

## 功能模块

| 文档 | 说明 |
|------|------|
| [对话与编排](modules/chat.md) | 端到端对话流、SSE 事件、引用系统、计划模式、子智能体、断线续播 |
| [提示词系统](modules/prompts.md) | 装配优先级、版本池、提示词广场、跨环境迁移 |
| [能力目录](modules/catalog.md) | catalog 单一真源、能力门控、用户自助能力 |
| [模型接入](modules/model-providers.md) | 模型供应商与角色体系、动态切换、个人 API-Key、计费 |
| [MCP 工具系统](modules/mcp-tools.md) | 8 个内置 Server、连接池、管理员/用户自定义 MCP |
| [技能系统](modules/agent-skills.md) | SKILL.md 机制、技能包分层、技能市场、技能蒸馏 |
| [沙箱执行](modules/sandbox.md) | 三种 provider、bash 工具、快照持久化、MySpace 直挂 |
| [记忆系统](modules/memory.md) | L1/L2/L3 三层记忆、脱敏、审计、mem0 基础设施 |
| [知识库](modules/knowledge-base.md) | 自建知识库（混合检索）与 Dify 外接、公共库管理 |
| [对象存储](modules/storage.md) | local / s3 / oss 三后端、产物仓、文件链路 |
| [项目空间与我的空间](modules/projects-myspace.md) | 项目工作区、个人/团队文件夹、文件入上下文 |
| [认证与权限](modules/auth.md) | AUTH_MODE 三模式、SSO、权限位、管理凭证 |
| [管理台](modules/admin-console.md) | /admin 与 /config 双管理台、19 组管理路由 |
| [自动化与批量执行](modules/automation.md) | 定时任务调度、计划模式、批量编排 |
| [数据画布与产物](modules/canvas-artifacts.md) | Univer 画布、代码产物、Artifact 中心、会话分享 |

## API 参考

| 文档 | 说明 |
|------|------|
| [API 总览](api/overview.md) | 统一响应信封、鉴权方式、SSE 协议、全量路由清单 |
| [错误码参考](api/error-codes.md) | 实装错误码全表、license 402、前端处理约定 |

## 版本（社区版 / 商业版）

| 文档 | 说明 |
|------|------|
| [社区版 vs 商业版](editions/overview.md) | open-core 模式、功能对比矩阵、三种运行形态、升级路径 |
| [License 机制](editions/license.md) | 离线签名验签、状态机、能力位执法、签发工具（商业版 EE） |
| [CE 构建管线](editions/build-ce.md) | manifest、build_ce.py 流水、overlay、验收闸门 |

## 开发指南

| 文档 | 说明 |
|------|------|
| [后端开发](development/backend.md) | Docker 内开发模式、测试、迁移、分层规范、新增路由/MCP/技能 |
| [前端开发](development/frontend.md) | 构建热替、目录规范、API 调用约定、edition 门控 |

---

内部设计与方案文档（CE/EE 拆分施工图、沙箱快照设计等）见 [docs 根目录](../README.md)。
