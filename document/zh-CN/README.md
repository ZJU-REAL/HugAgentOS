# HugAgentOS 中文文档

> 最后更新：2026-10-23 ｜ [English](../en/README.md)

HugAgentOS 是一个开源、可自托管的 Agent Harness：你提出的问题在云端持续执行，关掉电脑也不会中断；
在此之上提供 7×24 小时在线的 Personal Agent，并以 HugAgentOS Cloud（服务器）和 HugAgentOS Local
（桌面本机）两种形态运行。平台以开源社区版（CE）与商业版（EE）双形态发行。本目录是面向部署者、
使用者与二次开发者的完整产品文档。

## 快速上手

| 文档 | 说明 |
|------|------|
| [产品简介](getting-started/introduction.md) | Durable Agent、运行形态、能力总览、技术栈与整体架构 |
| [快速开始](getting-started/quick-start.md) | 选择 HugAgentOS Cloud、桌面客户端或一键安装，并启用 Personal Agent |
| [快速构建领域本体](getting-started/domain-ontology-quickstart.md) | 用最小 Domain Pack 跑通校验、发布、测试和工具绑定 |

## 部署

先看[部署指南（选择部署方式）](deployment/README.md)，再按方式查阅：

| 文档 | 说明 |
|------|------|
| [部署指南 · 总览](deployment/README.md) | 各部署方式对比与选型、部署后验证 |
| [Docker Compose 部署](deployment/docker-compose.md) | HugAgentOS Cloud 标准形态：服务拓扑、profiles、重建流程、数据库迁移 |
| [桌面离线本机模式](deployment/desktop-local-mode.md) | HugAgentOS Local：私有运行时、仅本机 / 仅云端 / 本机 + 云端、升级与回滚 |
| [本机项目工作目录](deployment/local-project-workspace.md) | 桌面端绑定本地项目后的执行目录与权限规则 |
| [本机与云端技能、插件管理](deployment/local-capability-management.md) | 混合模式下技能与插件的来源和管理 |
| [无 Docker 一键安装](deployment/quick-install.md) | 个人单机：SQLite + 进程内状态 + 子进程 MCP / 沙箱 |
| [Windows 安装与部署](deployment/windows-deployment.md) | 桌面端单机服务或 Docker Desktop + WSL2 |
| [macOS 签名与升级](deployment/macos-signing.md) | 桌面端签名、公证与升级 |
| [我的空间文件挂载](deployment/space-files.md) | 网页与沙箱共用的文件目录 |
| [环境变量参考](deployment/environment-variables.md) | 全量环境变量逐组说明（默认值 / 作用 / CE·EE 相关性） |

## 架构

| 文档 | 说明 |
|------|------|
| [总体架构](architecture/overview.md) | 分层架构、本体控制平面、一次对话的完整生命周期、容器拓扑 |
| [后端架构](architecture/backend.md) | `src/backend/` 目录详解与路由注册表 |
| [前端架构](architecture/frontend.md) | 入口分发、组件组、Zustand store、模块页面与应用中心 |
| [数据模型](architecture/data-model.md) | 表分组速览、迁移链、CE/EE 表边界 |

## 功能模块

**Harness 与长时运行**

| 文档 | 说明 |
|------|------|
| [对话与智能体编排](modules/chat.md) | Run 与连接解耦、断线续播、运行中转向、上下文按步恢复与压缩、计划模式、子智能体 |
| [Personal Agent](modules/personal-agent.md) | 7×24 个人智能体：后台任务、永久云电脑、定时与心跳、IM 渠道、审批、文件记忆与 Dreaming |
| [自主循环](modules/autonomous-loop.md) | 目标跨轮存活、maker≠checker、停滞护栏、断点续跑 |
| [工作流后台作业](modules/workflow-jobs.md) | 沙箱受管进程、租约接管、派生主智能体 |
| [自动化与计划模式](modules/automation.md) | 定时任务调度、计划模式 |

**能力与扩展**

| 文档 | 说明 |
|------|------|
| [MCP 工具系统](modules/mcp-tools.md) | 内置及插件 Server、连接池、自定义 MCP |
| [技能系统](modules/agent-skills.md) | SKILL.md 机制、技能包分层、技能市场 |
| [浏览器自动化插件](modules/browser-automation.md) | 浏览网页、操作表单与网页测试 |
| [能力目录](modules/catalog.md) | catalog 单一真源、能力门控、用户自助能力 |
| [沙箱执行](modules/sandbox.md) | provider、Bash 工具、快照持久化、我的空间直挂 |
| [模型接入](modules/model-providers.md) | 模型供应商与角色体系、动态切换、个人 API-Key |
| [提示词系统](modules/prompts.md) | 装配优先级、版本池、跨环境迁移 |

**知识、记忆与治理**

| 文档 | 说明 |
|------|------|
| [记忆系统](modules/memory.md) | L1/L2/L3 三层记忆、持久 Outbox、做法沉淀、脱敏与审计 |
| [领域本体治理与校验](modules/ontology-harness.md) | Domain Pack、工具门禁、交付前评审、演进草案人工审核 |
| [知识库](modules/knowledge-base.md) | 自建知识库（混合检索）与外接知识库 |
| [知识库 Wiki](modules/knowledge-base-wiki.md) | 抽取实体与概念，生成互链条目页与概念图谱 |

**工作区与产物**

| 文档 | 说明 |
|------|------|
| [项目空间与我的空间](modules/projects-myspace.md) | 项目工作区、个人文件夹、云文档在线编辑、文件入上下文 |
| [数据画布与产物](modules/canvas-artifacts.md) | Univer 画布、代码产物、产物中心、会话分享 |
| [站点](modules/sites.md) | 对话建站与平台托管 |
| [应用数据库与 MCP 托管](modules/application-hosting.md) | 应用独立数据库、表单数据与 MCP 发布 |
| [对象存储](modules/storage.md) | 本地与 S3 兼容存储、产物仓、文件链路 |

**平台管理**

| 文档 | 说明 |
|------|------|
| [认证与权限](modules/auth.md) | AUTH_MODE、SSO、权限位、管理凭证 |
| [管理台](modules/admin-console.md) | /admin 与 /config 管理台 |

## API 参考

| 文档 | 说明 |
|------|------|
| [API 总览](api/overview.md) | 统一响应信封、鉴权方式、SSE 协议、全量路由清单 |
| [错误码参考](api/error-codes.md) | 实装错误码全表、license 402、前端处理约定 |

## 版本（社区版 / 商业版）

| 文档 | 说明 |
|------|------|
| [社区版 vs 商业版](editions/overview.md) | open-core 模式、功能对比矩阵、运行形态、升级路径 |
| [License 机制](editions/license.md) | 离线签名验签、状态机、能力位执法（商业版 EE） |
| [CE 构建管线](editions/build-ce.md) | manifest、build_ce.py 流水、overlay、验收闸门 |

## 开发指南

| 文档 | 说明 |
|------|------|
| [后端开发](development/backend.md) | 开发模式、测试、迁移、分层规范、新增路由 / MCP / 技能 |
| [前端开发](development/frontend.md) | 构建、目录规范、API 调用约定、edition 门控 |
| [客户对话界面定制](development/frontend-customization.md) | 页面配置、客户组件包与独立前端 |

**工作台应用（Workbench）**

| 文档 | 说明 |
|------|------|
| [工作台协议](development/workbench-protocol.md) | JSON Schema、协议版本与接入方式 |
| [Workbench SDK 与插件页面](development/workbench-sdk.md) | SDK 1.0.0、UI v3 插件页面 |
| [开发预览](development/workbench-preview.md) | 独立工程与权限预览 |
| [构建与离线制品](development/workbench-build.md) | 协议、SDK 与宿主前端的构建交付 |
| [应用地址与插件导航](development/workbench-routing.md) | `/apps/` 地址与 applicationKey |
| [应用资源](development/workbench-resources.md) | 包内 resource runtime |
| [界面状态与有限保活](development/workbench-state.md) | 状态格式版本与保存恢复 |
| [角标、通知与后台运行](development/workbench-activity.md) | badge、notification、background 能力 |
| [平台适配](development/workbench-platform-adapters.md) | Web、Tauri 与 Electron 适配接口 |
| [来源与离线安装](development/workbench-origins.md) | 本机与云端同名插件的入口 |
| [插件升级与权限审批](development/workbench-plugin-upgrades.md) | 升级、重装与授权继承 |
| [应用目录与布局策略](development/workbench-access-policies.md) | 目录接口、可用性与默认入口 |
| [模块导航与应用目录](development/workbench-navigation-preferences.md) | 侧栏模块与应用中心 |
| [目录通知与撤销](development/workbench-invalidation.md) | ETag 与目录失效通知 |
| [普通外部应用](development/workbench-external-apps.md) | 外链应用的管理与使用 |
| [外部应用认证](development/workbench-external-auth.md) | 外部应用认证配置（商业版 EE 管理台） |
| [外部页面嵌入](development/workbench-external-embedding.md) | 嵌入工作区的 origin 与就绪确认 |
| [应用诊断](development/workbench-diagnostics.md) | 诊断编号与重试 |
| [错误码](development/workbench-errors.md) | 工作台错误码 |
| [管理员指南](development/workbench-administration.md) | 工作台管理 |
| [兼容与迁移](development/workbench-migration.md) | 版本兼容与迁移 |
