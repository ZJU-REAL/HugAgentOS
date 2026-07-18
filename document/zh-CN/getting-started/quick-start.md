# 快速开始

> 最后更新：2026-07-19 ｜
> [English](../../en/getting-started/quick-start.md)

使用一条命令在个人电脑上安装 HugAgentOS。本地单机版会运行 Web 应用、API、
SQLite 数据库、进程内状态、MCP 工具和本地子进程沙箱，不需要 Docker、
PostgreSQL 或 Redis。

## 前置条件

开始前安装下列工具，并准备一个大模型 API，或提供 OpenAI 兼容端点的本地模型。

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux、macOS，或通过 WSL2 使用 Windows |
| Python | 3.10 或更高版本 |
| Node.js | 20 或更高版本，并包含 npm |
| Git 与 curl | 可通过 `PATH` 直接调用 |

## 一键安装

在任意目录运行公开安装器：

```bash
curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
```

安装器会把最新社区版下载到 `~/.hugagent/source`，在
`~/.hugagent/venv` 创建隔离环境，安装依赖并构建 Web 应用，然后进入交互式
首次配置向导。

## 完成首次配置

按照终端提示创建管理员账号并配置对话模型。向量模型、重排模型、文件解析服务和
互联网搜索服务均为可选项，可以稍后再添加。

配置完成后，HugAgentOS 会自动启动并打开
[http://127.0.0.1:3001](http://127.0.0.1:3001)。

## 再次启动

以后需要启动应用时，运行已安装的命令：

```bash
~/.hugagent/venv/bin/hugagent
```

也可以把命令加入 Shell 的 `PATH`：

```bash
export PATH="$HOME/.hugagent/venv/bin:$PATH"
```

## 选择生产部署方式

一键安装适合个人试用和开发，默认采用单进程、SQLite、进程内会话和宿主机子进程
沙箱。团队协作、高可用或需要生产隔离时，请使用
[Docker Compose 部署指南](../deployment/docker-compose.md)。

## 下一步

首次登录成功后，可以继续阅读：

- [完整无 Docker 安装指南](../deployment/quick-install.md)：安装选项、能力边界和故障排查；
- [模型接入](../modules/model-providers.md)；
- [MCP 工具](../modules/mcp-tools.md)、
  [Agent Skills](../modules/agent-skills.md)和
  [私有知识库](../modules/knowledge-base.md)。
