<p align="center">
  <img
    src="./document/assets/hugagentos-readme-hero.png"
    alt="HugAgentOS——开源、自托管的智能体工作台"
    width="100%"
  />
</p>

<h1 align="center">HugAgentOS</h1>

<p align="center">
  <strong>开源、自托管的智能体工作台</strong>
</p>

<p align="center">
  让模型不只回答问题，还能检索知识、调用工具、处理文件、运行代码，
  并持续完成真实任务。
</p>

<p align="center">
  <a href="./README.md">English</a> ·
  <a href="./README_CN.md">简体中文</a>
</p>

<!-- 预留稳定的上线地址，官网与在线体验启用后无需重新调整 README 结构。 -->
<p align="center">
  <a href="https://hugagentos.com">官方网站</a> ·
  <a href="https://app.hugagentos.com">在线使用</a>
</p>

<p align="center">
  <a href="./LICENSE">
    <img src="https://img.shields.io/badge/License-Apache_2.0_%2B_terms-2E8B57?style=flat-square" alt="Apache 2.0 with supplementary terms" />
  </a>
  <a href="./document/zh-CN/editions/overview.md">
    <img src="https://img.shields.io/badge/Edition-Community-635BFF?style=flat-square" alt="Community Edition" />
  </a>
  <a href="./document/zh-CN/deployment/quick-install.md">
    <img src="https://img.shields.io/badge/Install-One_command-0F766E?style=flat-square" alt="一键安装" />
  </a>
  <a href="./document/zh-CN/architecture/overview.md">
    <img src="https://img.shields.io/badge/Agent-AgentScope_2.0-FF6A00?style=flat-square" alt="AgentScope 2.0" />
  </a>
  <a href="./document/zh-CN/modules/mcp-tools.md">
    <img src="https://img.shields.io/badge/Tools-MCP-111827?style=flat-square" alt="Model Context Protocol" />
  </a>
</p>

HugAgentOS 把智能对话、知识库 RAG、子智能体、MCP 工具、Agent Skills、
沙箱执行、长期记忆、自动化和数据画布整合到一个可私有部署的 Web 工作台。
你可以接入自己的模型和数据，从一次对话开始，逐步搭建真正属于自己的智能体系统。

<p align="center">
  <img
    src="./document/assets/hugagentos-product-overview.png"
    alt="HugAgentOS 对话、工具调用、知识库与产物工作台概览"
    width="100%"
  />
</p>

> [!NOTE]
> 本社区仓库由上游主仓按发布版本自动生成，并标记为 `generated`。
> `src/**` 的修改请通过 Issue 或 Discussion 反馈；文档与示例欢迎直接提交 PR。
> 详细规则见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 快速开始

在 Linux、macOS 或 WSL2 上使用一条命令安装个人单机版。开始前需要
Python 3.10+、Node.js 20+、Git，以及一个可用的大模型 API；不需要
Docker、PostgreSQL 或 Redis。

```bash
curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
```

安装器会把 HugAgentOS 拉取到 `~/.hugagent/source`，创建隔离的 Python 环境，
安装依赖并构建 Web 应用，然后进入首次配置向导。按照提示创建管理员并接入
OpenAI 兼容模型或本地模型，完成后浏览器会打开
[http://127.0.0.1:3001](http://127.0.0.1:3001)。

以后可随时使用下面的命令再次启动：

```bash
~/.hugagent/venv/bin/hugagent
```

> [!NOTE]
> 一键安装适合个人试用与开发，默认使用 SQLite、进程内状态和本地子进程沙箱。
> 团队或生产环境请使用
> [Docker Compose 部署指南](./document/zh-CN/deployment/docker-compose.md)。

安装选项、能力边界和故障排查见
[无 Docker 一键安装指南](./document/zh-CN/deployment/quick-install.md)。

## 从回答到交付

HugAgentOS 的重点不是再包装一个聊天界面，而是把智能体完成任务需要的上下文、
执行能力和产物管理放在同一条链路中。

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>🔌 模型可替换</strong><br />
      通过统一的模型服务配置接入云端或本地模型，不把应用锁定在单一供应商。
    </td>
    <td width="50%" valign="top">
      <strong>🛠️ 能执行任务</strong><br />
      ReAct 编排 MCP、技能和沙箱，让模型能够搜索、分析、生成文件并调用外部能力。
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🧠 有知识与记忆</strong><br />
      私有知识库与分层记忆共同提供跨文件、跨会话的长期上下文。
    </td>
    <td width="50%" valign="top">
      <strong>🏠 数据可自持</strong><br />
      应用、数据库和文件存储均可运行在自己的基础设施中，部署边界清晰可控。
    </td>
  </tr>
</table>

## 核心能力

社区版覆盖个人智能体从对话、执行到沉淀和复用的完整闭环；可选组件按需启用，
不需要在首跑时部署全部基础设施。

<table>
  <tr>
    <td width="50%" valign="top">
      <strong>💬 智能对话与计划模式</strong><br />
      SSE 流式输出、ReAct 工具编排、深度思考、计划模式、引用溯源与断线续播。
    </td>
    <td width="50%" valign="top">
      <strong>📚 私有知识库 RAG</strong><br />
      文档上传与分块、向量和关键词混合检索、可选重排，以及个人知识库隔离。
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🤝 个人子智能体</strong><br />
      创建不同角色的子智能体，通过自动路由或 <code>@</code> 提及协同完成任务。
    </td>
    <td width="50%" valign="top">
      <strong>🔧 MCP 工具生态</strong><br />
      内置联网搜索、网页抓取、知识检索、图表、报告、批量任务、自动化和技能管理。
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🧩 Agent Skills</strong><br />
      使用标准化技能说明与脚本扩展智能体，支持内置技能、技能市场和个人技能。
    </td>
    <td width="50%" valign="top">
      <strong>⚙️ 自动化与批量执行</strong><br />
      用自然语言创建定时任务，或对 Excel、Word、文件列表批量运行同一套流程。
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🧪 沙箱与内容产物</strong><br />
      在本地子进程或轻量容器沙箱中运行代码，生成图表、报告、Office 文件、网页和数据画布。
    </td>
    <td width="50%" valign="top">
      <strong>🧠 三层个人记忆</strong><br />
      L1 个人画像使用关系库存储，可选启用 Milvus 向量记忆与 Neo4j 图谱记忆。
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <strong>🗂️ 个人工作空间</strong><br />
      用项目、文件夹、收藏、会话分享和产物中心组织长期任务与资料。
    </td>
    <td width="50%" valign="top">
      <strong>📊 数据画布</strong><br />
      在会话中查看和编辑结构化数据，让分析过程与最终结果保持在同一工作区。
    </td>
  </tr>
</table>

## 系统架构

HugAgentOS 将 Web 接入、对话运行、智能体编排和工具执行分层解耦。本地单机版
使用 SQLite 与进程内状态，多用户部署使用 PostgreSQL 与 Redis；Milvus 与
Neo4j 作为可选记忆组件加入。

```mermaid
flowchart LR
    U[浏览器] --> FE[React 19 + Nginx]
    FE --> API[FastAPI API]
    API --> RUN[ChatRun + Redis Stream]
    RUN --> WF[流式工作流编排]
    WF --> AGENT[AgentScope 2.0 ReActAgent]

    AGENT --> MCP[8 个通用 MCP 工具]
    AGENT --> SKILL[Agent Skills]
    AGENT --> BOX[轻量脚本沙箱]
    WF --> RAG[私有知识库 RAG]
    WF --> MEM[L1 / L2 / L3 记忆]

    API --> PG[(SQLite / PostgreSQL)]
    RUN --> REDIS[(进程内状态 / Redis)]
    API --> STORE[(本地文件存储)]
    RAG -. 可选 .-> MILVUS[(Milvus)]
    MEM -. 可选 .-> MILVUS
    MEM -. 可选 .-> NEO4J[(Neo4j)]
```

### 技术栈

项目选择成熟、可替换的开源组件，并通过清晰的服务边界组合为完整产品。

| 层级 | 主要技术 |
|---|---|
| 智能体运行时 | AgentScope 2.0、ReAct、Model Context Protocol |
| 后端 | Python、FastAPI、SQLAlchemy、Alembic |
| 前端 | React 19、TypeScript、Vite、Zustand、Ant Design |
| 数据与状态 | SQLite 或 PostgreSQL 15、进程内状态或 Redis 7、本地文件存储 |
| 可选记忆 | Milvus 2.4、Neo4j 5 Community、mem0 |
| 部署 | 本地一键安装、Docker Compose、Nginx |

更详细的请求生命周期、容器拓扑和关键设计决策见
[架构总览](./document/zh-CN/architecture/overview.md)。

## 社区版与商业版

社区版让个人把智能体能力完整运行起来；商业版在相同产品体验上补充组织级治理、
协作和交付能力。商业版能力不会以关闭开关的形式混入社区版源码。

| 社区版（CE） | 商业版（EE）新增 |
|---|---|
| 智能对话、计划模式与个人子智能体 | 团队、成员、组织级智能体库与权限矩阵 |
| 8 个通用 MCP、个人技能与技能市场 | 行业数据工具、组织级能力治理与技能审核 |
| 私有知识库、三层个人记忆 | 公共知识库管理与记忆审计 |
| 自动化、批量执行、个人数据画布 | 组织计费、用量汇总与画布多人协同 |
| 轻量沙箱、本地文件存储 | 持久沙箱、云存储与离线商业交付 |
| 本地账号与保留 Powered-by 的品牌配置 | SSO、审计合规与完整 white-label |

功能边界与升级路径以
[社区版与商业版说明](./document/zh-CN/editions/overview.md)为准。

## 文档

仓库内提供完整的中英文文档，从首次部署到架构和二次开发均可离线阅读。

| 你想了解 | 中文文档 | English |
|---|---|---|
| 项目定位与整体能力 | [产品简介](./document/zh-CN/getting-started/introduction.md) | [Introduction](./document/en/getting-started/introduction.md) |
| 10 分钟运行项目 | [快速开始](./document/zh-CN/getting-started/quick-start.md) | [Quick start](./document/en/getting-started/quick-start.md) |
| 生产部署与环境变量 | [部署指南](./document/zh-CN/deployment/README.md) | [Deployment](./document/en/deployment/README.md) |
| 系统设计与请求链路 | [架构总览](./document/zh-CN/architecture/overview.md) | [Architecture](./document/en/architecture/overview.md) |
| MCP、技能、记忆与沙箱 | [功能模块](./document/zh-CN/README.md#功能模块) | [Modules](./document/en/README.md#modules) |
| 后端与前端开发 | [开发指南](./document/zh-CN/README.md#开发指南) | [Development](./document/en/README.md#development) |

所有文档入口见 [document/README.md](./document/README.md)。

## 参与贡献

我们欢迎问题反馈、功能建议、文档改进和可复现的补丁。提交前请先阅读
[贡献指南](./CONTRIBUTING.md)，了解生成代码与可直接修改内容的边界。

- 报告 Bug 时，请附复现步骤、期望行为、实际行为和运行环境。
- 提议功能时，请说明具体使用场景，以及它解决了什么问题。
- 修改文档或示例时，请保持中英文内容与 CE/EE 边界一致。

发现安全漏洞时不要创建公开 Issue。请按照
[安全策略](./SECURITY.md)提供的私密渠道报告。

## 许可证

HugAgentOS Community Edition 采用 Apache License 2.0 并附加补充条款。
补充条款限制将本软件作为竞争性的多租户 SaaS 转售，并要求保留界面中的
Powered-by 标识。内部使用、修改和分发的完整权利与义务以
[LICENSE](./LICENSE)和 [NOTICE](./NOTICE)为准。
