# HugAgentOS 文档 / Documentation

> 最后更新 / Last updated：2026-10-23

HugAgentOS 是一个开源、可自托管的 Agent Harness：你提出的问题在云端持续执行，关掉电脑也不会中断；在此之上提供 7×24 小时在线的 Personal Agent，并以 HugAgentOS Cloud（服务器）和 HugAgentOS Local（桌面本机）两种形态运行，以 open-core 模式提供 **开源社区版（CE）** 与 **商业版（EE）**。产品文档按语言分树维护：

HugAgentOS is an open-source, self-hosted agent harness: your request keeps running in the cloud even after you close your computer. On top of it, HugAgentOS provides an always-on Personal Agent and runs as HugAgentOS Cloud (on your server) or HugAgentOS Local (on your desktop), shipped in an open-core model as a **Community Edition (CE)** and an **Enterprise Edition (EE)**. Product documentation is maintained per language:

| 语言 / Language | 入口 / Entry |
|---|---|
| 简体中文 | [zh-CN/README.md](./zh-CN/README.md) |
| English | [en/README.md](./en/README.md) |

两棵树文件名一一对应，内容互为镜像。Both trees mirror each other file-by-file.

## 文档结构 / Structure

```
document/
├── zh-CN/ · en/             # 双语产品文档（镜像）
│   ├── getting-started/     # 产品简介、快速开始、领域本体快速入门
│   ├── deployment/          # 部署总览、Docker Compose、桌面本机模式、一键安装、Windows / macOS / UOS、环境变量
│   ├── architecture/        # 总体架构、后端、前端、数据模型
│   ├── modules/             # 功能模块：对话与 Run、Personal Agent、自主循环、后台作业、MCP、技能、沙箱、
│   │                        #   记忆、本体治理、知识库、站点与应用托管、存储、项目空间、认证、管理台等
│   ├── api/                 # API 总览（信封 / 鉴权 / SSE / 路由清单）、错误码
│   ├── editions/            # 社区版 vs 商业版、License 机制、CE 构建管线
│   └── development/         # 后端 / 前端开发、界面定制、工作台（Workbench）协议与 SDK
└── internal/                # ⚠️ 内部文档（仅主仓，不随社区版分发）
    ├── agents/              # 开发工作流约定（development-flow / issue-tracker /
    │                        #   triage-labels / domain），被 .agents/skills 与 CLAUDE.md 引用
    ├── harness/             # Harness 引擎迁移与交付验证手册
    ├── development-guide/   # 二次开发、插件 / 技能开发、商业版部署
    ├── evaluation/          # 内部评测方案与调研
    ├── 操作手册/            # 品牌操作手册（docx，含客户信息）
    └── *.md / *.html        # 内部方案、设计稿、排查与验证报告（按主题平铺）
```

## 开源边界 / Open-source boundary

- `zh-CN/`、`en/` 与本索引是**公开产品文档**：随社区版（CE）派生树一并开源。商业版（EE）独有能力在文中以「商业版 EE」/ "(EE)" 显式标注——这与 Dify / FastGPT 在公开文档中介绍企业版能力的做法一致。公开树内**不得**出现内部 IP、凭据、客户信息与商业策略内容。
- `internal/` 是**内部文档**（商业方案、拆分施工图、内部设计稿）：由 `ce/manifest.yaml` 的 `exclude: internal design docs` 保证永不进入社区版派生树。新增内部文档一律放这里。

`zh-CN/`, `en/`, and this index are **public product docs**, shipped with the Community Edition derived tree; EE-only capabilities are explicitly labeled. `internal/` holds **internal documents** (business strategy, split blueprints, design drafts) and is excluded from the CE tree via `ce/manifest.yaml` — put any new internal document there.

## 常用专题 / Frequently referenced topics

| 专题 / Topic | 中文 | English |
|---|---|---|
| Personal Agent | [Personal Agent](zh-CN/modules/personal-agent.md) | [Personal Agent](en/modules/personal-agent.md) |
| 桌面本机与混合模式 / Desktop local and hybrid modes | [桌面离线本机模式](zh-CN/deployment/desktop-local-mode.md) | [Desktop local mode](en/deployment/desktop-local-mode.md) |
| 云文档在线编辑 / Online cloud document editing | [我的空间](zh-CN/modules/projects-myspace.md#云文档在线编辑) | [Projects & My Space](en/modules/projects-myspace.md#online-cloud-document-editing) |
| 项目与智能体独立对话链接 / Standalone chat links | [对话](zh-CN/modules/chat.md#项目和智能体的独立对话链接) | [Chat](en/modules/chat.md#standalone-chat-links-for-projects-and-agents) |
| 模块页面滚动与应用中心 / Module scrolling and app center | [前端架构](zh-CN/architecture/frontend.md#模块页面滚动与应用中心) | [Frontend](en/architecture/frontend.md#module-page-scrolling-and-app-center) |
| 工作台协议与 SDK / Workbench protocol and SDK | [工作台文档](zh-CN/README.md#开发指南) | [Workbench docs](en/README.md#development) |
