# 快速开始

> 最后更新：2026-07-21 ｜
> [English](../../en/getting-started/quick-start.md)

个人试用可以选择一键安装，长期服务或服务隔离场景可以选择 Docker Compose。
两种方式都需要准备 OpenAI 兼容 API 或本地模型。

## 选择部署方式

两种部署方式使用不同的运行与持久化模型。

| 方式 | 适用场景 | 运行形态 |
|---|---|---|
| 一键安装 | 个人试用与开发 | SQLite、进程内状态、本地子进程沙箱 |
| Docker Compose | 长期运行服务器与服务隔离 | PostgreSQL、Redis、容器沙箱、持久化服务卷 |

## 方式一：一键安装

在 Linux、macOS 或通过 WSL2 使用 Windows 时选择此方式。开始前安装下列工具。

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux、macOS，或通过 WSL2 使用 Windows |
| Python | 3.11 或更高版本 |
| Node.js | 20 或更高版本，并包含 npm |
| Git 与 curl | 可通过 `PATH` 直接调用 |
| Rust 与 Cargo | 仅 Linux 没有兼容 `ripgrep` 预编译 wheel 时需要，包括 glibc 低于 2.39 的系统 |

### 执行安装

在任意目录运行公开安装器：

```bash
curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash
```

安装器会把最新社区版下载到 `~/.hugagent/source`，在
`~/.hugagent/venv` 创建隔离环境，安装依赖并构建 Web 应用，然后进入交互式
首次配置向导。

### 完成首次配置

按照终端提示创建管理员账号并配置对话模型。向量模型、重排模型、文件解析服务和
互联网搜索服务均为可选项，可以稍后再添加。

配置完成后，HugAgentOS 会自动启动并打开
[http://127.0.0.1:3001](http://127.0.0.1:3001)。

### 再次启动

以后需要启动应用时，运行已安装的命令：

```bash
~/.hugagent/venv/bin/hugagent
```

也可以把命令加入 Shell 的 `PATH`：

```bash
export PATH="$HOME/.hugagent/venv/bin:$PATH"
```

## 方式二：Docker Compose

需要持久化服务卷和容器隔离时选择此方式。安装 Git、Docker Engine 或
Docker Desktop 和 Docker Compose v2，然后运行：

```bash
git clone https://github.com/ZJU-REAL/HugAgentOS.git
cd HugAgentOS
cp .env.example .env
mkdir -p data/storage
docker compose up -d --build
```

打开 [http://localhost:3002](http://localhost:3002)，使用 `admin` / `admin`
登录并修改密码，然后进入「设置 → 系统管理 → 模型服务」接入模型。使用
`docker compose ps` 检查服务，使用 `docker compose down` 停止服务且保留数据。

Profiles、数据持久化、生产配置和重建流程见
[Docker Compose 部署指南](../deployment/docker-compose.md)。

## 下一步

首次登录成功后，可以继续阅读：

- [完整无 Docker 安装指南](../deployment/quick-install.md)：安装选项、能力边界和故障排查；
- [模型接入](../modules/model-providers.md)；
- [领域本体快速入门](domain-ontology-quickstart.md)：用最小 Domain Pack 构建受治理工作流；
- [MCP 工具](../modules/mcp-tools.md)、
  [Agent Skills](../modules/agent-skills.md)和
  [私有知识库](../modules/knowledge-base.md)。
