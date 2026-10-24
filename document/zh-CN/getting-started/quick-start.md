# 快速开始

> 最后更新：2026-10-23 ｜
> [English](../../en/getting-started/quick-start.md)

HugAgentOS 有三种上手方式。需要智能体 7×24 小时在云端持续运行时选择 HugAgentOS Cloud；
想在自己的电脑上离线使用时选择 HugAgentOS Local 桌面客户端；做二次开发或快速试用时选择
一键安装。所有方式都需要准备 OpenAI 兼容 API 或本地模型。

## 选择部署方式

| 方式 | 适用场景 | 运行形态 |
|---|---|---|
| HugAgentOS Cloud（Docker Compose） | 7×24 运行、Personal Agent、IM 渠道、多设备访问 | PostgreSQL、Redis、OpenSandbox、持久化卷 |
| HugAgentOS Local（桌面客户端） | 离线使用、敏感数据、处理本地项目 | 自带私有运行时、本地 SQLite 与文件 |
| 一键安装 | 试用与二次开发 | SQLite、进程内状态、本地子进程沙箱 |

**初始账号与密码均为 `admin`，首次登录必须修改；社区版不提供自助注册。**

## 方式一：HugAgentOS Cloud（Docker Compose）

安装 Git、Docker Engine 或 Docker Desktop 和 Docker Compose v2，然后运行：

```bash
git clone https://github.com/ZJU-REAL/HugAgentOS.git
cd HugAgentOS
cp .env.example .env
mkdir -p data/storage
docker compose build backend
docker compose up -d --build
```

打开 [http://localhost:3002](http://localhost:3002)，使用 `admin` / `admin`
登录并修改密码，然后进入「设置 → 系统管理 → 模型服务」接入模型。使用
`docker compose ps` 检查服务，使用 `docker compose down` 停止服务且保留数据。

Profiles、数据持久化、生产配置和重建流程见
[Docker Compose 部署指南](../deployment/docker-compose.md)。

### 启用 Personal Agent（可选）

Personal Agent 默认关闭。它使用独立的 OpenSandbox 控制器为每个用户维护一台永久云电脑。

1. 构建专用电脑镜像：

   ```bash
   docker build -f docker/personal-agent/Dockerfile.personal-agent -t hugagent-personal-agent:v5 .
   ```

2. 在 `.env` 中设置：

   | 变量 | 值 |
   |---|---|
   | `PERSONAL_AGENT_ENABLED` | `true` |
   | `HOST_STORAGE_PATH` | 宿主机上的绝对路径 |
   | `PERSONAL_AGENT_OPENSANDBOX_API_KEY` | 至少 32 个字符的独立随机密钥，例如 `openssl rand -hex 32` 的输出 |

3. 使用附加 Compose 文件启动：

   ```bash
   docker compose -f docker-compose.yml -f docker/personal-agent/compose.personal-agent.yml --profile personal-agent up -d
   ```

启动后，在「新建对话」下方点击「Personal Agent」即可使用。数据库迁移、镜像升级和渠道绑定见
[Personal Agent](../modules/personal-agent.md)。密钥不要提交到 Git。

## 方式二：HugAgentOS Local（桌面客户端）

1. 从 [Releases](https://github.com/ZJU-REAL/HugAgentOS/releases) 下载对应平台
   （Windows x86_64、macOS Apple Silicon / Intel、Linux x86_64）的桌面安装包。
2. 首次启动时选择运行模式：
   - **仅本机**：服务、数据和执行都在这台电脑上，可完全离线运行；
   - **仅云端**：连接已部署的 HugAgentOS Cloud；
   - **本机 + 云端**：使用云端账号，云端项目存云端，本地项目在本机执行；
     Personal Agent 可以借此连接这台电脑。
3. 选择本机模式时，客户端会校验并解压自带的运行时，通常数十秒内完成，不需要安装 Python、
   Docker 或数据库。

安装、升级回滚和数据目录见[桌面离线本机模式](../deployment/desktop-local-mode.md)；
绑定本地项目后的执行目录规则见[本机项目工作目录](../deployment/local-project-workspace.md)。

## 方式三：一键安装

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
~/.hugagent/venv/bin/hugagent-server
```

也可以把命令加入 Shell 的 `PATH`：

```bash
export PATH="$HOME/.hugagent/venv/bin:$PATH"
```

一键安装不提供跨进程的租约接管，不适合 7×24 运行。

## 下一步

首次登录成功后，可以继续阅读：

- [产品简介](introduction.md)：Durable Agent、运行形态与能力总览；
- [模型接入](../modules/model-providers.md)；
- [Personal Agent](../modules/personal-agent.md)与[自主循环](../modules/autonomous-loop.md)；
- [MCP 工具](../modules/mcp-tools.md)、
  [Agent Skills](../modules/agent-skills.md)和
  [私有知识库](../modules/knowledge-base.md)；
- [领域本体快速入门](domain-ontology-quickstart.md)：用最小 Domain Pack 构建受治理工作流；
- [完整无 Docker 安装指南](../deployment/quick-install.md)：安装选项、能力边界和故障排查。
