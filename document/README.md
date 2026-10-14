# HugAgentOS 文档 / Documentation

> 最后更新：2026-07-21

HugAgentOS 是一个企业级 AI Agent 平台（FastAPI + React + AgentScope 2.0），以 open-core 模式提供 **开源社区版（CE）** 与 **商业版（EE）** 双形态。产品文档按语言分树维护：

HugAgentOS is an enterprise-grade AI agent platform (FastAPI + React + AgentScope 2.0), shipped in an open-core model as a **Community Edition (CE)** and an **Enterprise Edition (EE)**. Product documentation is maintained per language:

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
│   ├── deployment/          # 部署总览(选型) + 无 Docker 一键安装 / Docker Compose / 离线生产 / Windows(WSL2) / 环境变量参考
│   ├── architecture/        # 总体架构、后端、前端、数据模型
│   ├── modules/             # 17 个功能模块文档（对话/提示词/能力目录/模型/
│   │                        #   MCP 工具/技能/沙箱/记忆/知识库/存储/项目空间/
│   │                        #   认证/管理台/自动化/画布与产物）
│   ├── api/                 # API 总览（信封/鉴权/SSE/路由清单）、错误码
│   ├── editions/            # 社区版 vs 商业版、License 机制、CE 构建管线
│   └── development/         # 后端 / 前端开发指南
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

云文档在线编辑：Markdown、TXT 和代码文件的编辑、保存、权限与版本冲突处理见
[我的空间](zh-CN/modules/projects-myspace.md#云文档在线编辑) /
[Online cloud document editing](en/modules/projects-myspace.md#online-cloud-document-editing)。
