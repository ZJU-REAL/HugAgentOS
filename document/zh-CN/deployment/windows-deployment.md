# Windows 安装与部署

> 最后更新：2026-07-23 ｜ [English](../../en/deployment/windows-deployment.md) ｜ 返回 [部署指南](README.md)

Windows 用户可以选择桌面端托管的无 Docker 单机服务，也可以通过 Docker Desktop + WSL2
运行团队版 Compose 服务。前者适合个人使用，后者适合多人协作与生产。

## 桌面端一键安装本机服务

Windows x86_64 个人用户可以直接使用 NSIS 桌面安装包。安装包携带与客户端同版本、通过
社区版边界检查的单文件 CE 服务压缩包；首次启动时解压资源，并在当前用户目录创建独立
Python 环境，不要求 Docker Desktop、WSL2、PostgreSQL 或 Redis。

### 前提

本机服务安装需要满足以下条件：

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 或 Windows 11，x86_64 |
| WebView2 | Windows 11 已内置；Windows 10 缺失时由 Tauri 安装器处理 |
| 网络 | 首次安装需下载 Python wheels 与可选 Node 工具；服务代码和 Web 前端已在安装包内 |
| Python | 已有 Python 3.11+ 可直接复用；缺失时安装器优先通过 `winget` 为当前用户安装 |
| Node.js | Node.js 20+ 缺失时安装器会尝试通过 `winget` 补齐；失败不阻断核心服务 |
| 磁盘 | 建议至少保留 5 GB，用于 Python 环境、模型工具依赖、数据和日志 |

### 安装步骤

按以下步骤完成个人单机安装：

1. 运行 HugAgentOS 的 NSIS `.exe` 安装包。
2. 在“是否同时安装无 Docker 的本机服务”提示中选择“是”。
3. 启动桌面客户端，等待服务设置页完成资源解压、依赖安装和健康检查。
4. 使用首次启动生成的 `admin` / `admin` 登录，并按提示立即修改密码。
5. 在首次引导中配置可用的大模型服务。

安装失败时，服务设置页会保留最近的安装日志。修复网络、Python 或磁盘问题后，选择
**一键安装并启动**即可幂等重试，不需要重新安装桌面客户端。

### 运行与数据

本机服务只监听 `http://127.0.0.1:32101`，不会向局域网暴露。客户端退出时会回收服务进程；
最小化到托盘时服务继续运行，以支持后台自动化任务。

数据与运行环境位于：

```text
%LOCALAPPDATA%\com.hugagent.desktop\local-server\
  data\                    SQLite、存储、工作区和持久日志
  runtime\
    source\                与桌面版本匹配的 CE 服务资源
    venv\                  独立 Python 环境
    node\                  可重新生成的 Node 工具与浏览器
    installed-bundle.json 已安装版本标记
  logs\                    桌面安装器与服务托管日志
  server.pid               崩溃后安全接管/回收服务进程的 PID 标记
```

桌面客户端更新后，如果随包服务版本变化，客户端会自动升级 `source` 和 Python 依赖，保留
`data`。交互卸载会询问是否同时删除本机服务数据，并默认选择“否”：选择“否”会保留账号、对话、
上传文件和工作区，选择“是”会一并清理。静默自动更新始终保留数据。卸载器会先停止本机服务，再把
待删除目录原子改名，
由隐藏的系统进程在后台执行清理，因此卸载界面不等待 Python 和 Node 的大量小文件逐个删除。

在桌面菜单中使用 **文件 → 设置服务器地址…** 可切换到团队服务器；使用
**文件 → 本机服务…** 可重新安装、启动或切回本机服务。

> **注意：** 本机服务与[无 Docker 一键安装](quick-install.md)共用单进程本地运行形态，但 Windows
> 的可选宿主工具能力可能降级：Node.js 自动安装失败时 React 建站与高级 PDF 渲染不可用，原生
> Windows 暂不提供 Milvus Lite 向量知识库。多人协作、生产、高可用和完整容器沙箱仍须使用
> Docker Compose。

## Docker Desktop + WSL2 团队部署

HugAgentOS 的标准团队部署形态是 Linux 容器编排。在 Windows 主机上，使用
**Docker Desktop + WSL2**：全部服务仍以 Linux 容器运行，Windows 只是宿主。下面列出与
Linux 部署的差异点和必要配置。

除桌面客户端自动管理的单机形态外，不支持手工在 Windows 原生 Python 环境中启动团队后端。

### 前提

| 项 | 要求 |
|---|---|
| CPU 架构 | 仅 x86_64（amd64）。镜像内多处二进制与上游沙箱镜像均为 linux/amd64，ARM Windows 不支持 |
| Docker Desktop | 启用 WSL2 backend，并在 Settings → Resources → WSL Integration 中启用目标发行版 |
| WSL2 发行版 | 任一主流 Linux 发行版（如 Ubuntu 22.04+），部署操作全部在其 bash 中进行 |
| Compose | Docker Desktop 自带 v2 插件（`docker compose`），满足要求 |

### 关键原则：一切放 WSL2 原生文件系统

仓库和数据目录必须放在 WSL2 的 ext4 文件系统内（如 `/home/<user>/`），**不要**放在 Windows 盘（`/mnt/c/...`）：

- `/mnt/c` 经 9P 协议转发，小文件 IO 性能差一个数量级，后端源码挂载与沙箱存储都会明显变慢；
- 沙箱功能会以「宿主绝对路径」做 bind-mount 与路径白名单校验，Windows 盘符路径在容器侧无效；
- `/mnt/c` 上的文件事件（inotify）不可靠。

```bash
# 在 WSL2 bash 内
git clone <repo-url> ~/HugAgentOS
cd ~/HugAgentOS
```

### 换行符（CRLF）

仓库自带 `.gitattributes`，对容器内执行的脚本、模板、Dockerfile、compose 文件强制 LF，正常克隆即可。若使用旧克隆或全局改过 git 配置，建议在 WSL2 内额外设置：

```bash
git config core.autocrlf input
```

> 症状对照：容器启动报 `bash\r: No such file or directory` 或 nginx `[emerg]` 配置解析失败，即为脚本/模板被转成了 CRLF——重新按 LF 检出即可。

### `.env` 差异项

在 `.env.example` 基础上，以下变量在 Windows/WSL2 下须特别注意：

| 变量 | Windows/WSL2 取值 |
|---|---|
| `HOST_REPO_PATH` | 仓库在 WSL2 内的绝对路径，如 `/home/<user>/HugAgentOS` |
| `HOST_STORAGE_PATH` | WSL2 内的绝对路径，如 `/home/<user>/hugagent-storage`（先创建目录） |
| `DOCKER_GID` | 在 WSL2 内执行 `stat -c '%g' /var/run/docker.sock` 取实际 GID（Docker Desktop 下通常不是默认的 999） |

首次部署前创建存储目录并保证容器内用户（UID 1000）可写：

```bash
mkdir -p ~/hugagent-storage
sudo chown -R 1000:1000 ~/hugagent-storage
```

### 沙箱 provider 选择

- **使用默认的 `script_runner`**（profile `script_runner`）：单容器 sidecar，无宿主路径二次转发，Windows 下可正常工作。
- **不要使用 `opensandbox`（商业版 EE）**：它经宿主 `docker.sock` 动态创建嵌套沙箱容器，并要求「backend 容器内路径 == 宿主 daemon 视角路径」严格一致；Docker Desktop 的 daemon 运行在独立的 `docker-desktop` 发行版中，文件系统视角与用户发行版不一致，路径白名单校验与 bind-mount 大概率失败。该 provider 仅建议在 Linux 主机使用。

### 启动

全部命令在 WSL2 bash 内执行（`make`、`scripts/deploy/*.sh` 均为 bash 脚本，PowerShell/CMD 无法直接运行）：

```bash
docker compose --profile script_runner up -d --build
```

验证：

```bash
docker compose ps
curl -fsS http://localhost:3000/api/health
```

### 可选：mem0 记忆基础设施

`mem0` profile（Milvus + etcd + MinIO + Neo4j）内存占用大。Windows 下建议先在 `%UserProfile%\.wslconfig` 提高 WSL2 内存上限（如 `memory=12GB`），再启用：

```bash
docker compose --profile mem0 up -d
```

### 已知限制汇总

| 项 | 说明 |
|---|---|
| 手工在 Windows 原生运行团队后端 | 不支持；个人使用请用桌面端托管的本机服务 |
| `opensandbox` provider（EE） | Docker Desktop 下不可用，用 `script_runner` |
| ARM Windows | 不支持（amd64-only 镜像与二进制） |
| 仓库/存储放 `/mnt/c` | 不支持（性能与路径语义问题），必须放 WSL2 文件系统 |
| 管理后台「沙盒依赖重建」 | 依赖 `DOCKER_GID` 正确配置；GID 不对时该功能优雅降级，不影响其他功能 |
