# Windows 部署（Docker Desktop / WSL2）

> 最后更新：2026-07-16 ｜ [English](../../en/deployment/windows-deployment.md) ｜ 返回 [部署指南](README.md)

> **适用场景**：在 **Windows 宿主**上以 Docker Desktop + WSL2 运行 [Docker Compose 部署](docker-compose.md)；本文只讲与 Linux 部署的差异点。

HugAgentOS 的标准部署形态是 Linux 容器编排（见《Docker Compose 部署指南》）。在 Windows 主机上，推荐且唯一受支持的方式是 **Docker Desktop + WSL2**：全部服务仍以 Linux 容器运行，Windows 只是宿主。本文列出与 Linux 部署的差异点和必要配置。

**不支持** 脱离 Docker 在 Windows 原生 Python 环境直接运行后端（存在 POSIX 专属依赖与容器路径假设）。

## 前提

| 项 | 要求 |
|---|---|
| CPU 架构 | 仅 x86_64（amd64）。镜像内多处二进制与上游沙箱镜像均为 linux/amd64，ARM Windows 不支持 |
| Docker Desktop | 启用 WSL2 backend，并在 Settings → Resources → WSL Integration 中启用目标发行版 |
| WSL2 发行版 | 任一主流 Linux 发行版（如 Ubuntu 22.04+），部署操作全部在其 bash 中进行 |
| Compose | Docker Desktop 自带 v2 插件（`docker compose`），满足要求 |

## 关键原则：一切放 WSL2 原生文件系统

仓库和数据目录必须放在 WSL2 的 ext4 文件系统内（如 `/home/<user>/`），**不要**放在 Windows 盘（`/mnt/c/...`）：

- `/mnt/c` 经 9P 协议转发，小文件 IO 性能差一个数量级，后端源码挂载与沙箱存储都会明显变慢；
- 沙箱功能会以「宿主绝对路径」做 bind-mount 与路径白名单校验，Windows 盘符路径在容器侧无效；
- `/mnt/c` 上的文件事件（inotify）不可靠。

```bash
# 在 WSL2 bash 内
git clone <repo-url> ~/HugAgentOS
cd ~/HugAgentOS
```

## 换行符（CRLF）

仓库自带 `.gitattributes`，对容器内执行的脚本、模板、Dockerfile、compose 文件强制 LF，正常克隆即可。若使用旧克隆或全局改过 git 配置，建议在 WSL2 内额外设置：

```bash
git config core.autocrlf input
```

> 症状对照：容器启动报 `bash\r: No such file or directory` 或 nginx `[emerg]` 配置解析失败，即为脚本/模板被转成了 CRLF——重新按 LF 检出即可。

## `.env` 差异项

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

## 沙箱 provider 选择

- **使用默认的 `script_runner`**（profile `script_runner`）：单容器 sidecar，无宿主路径二次转发，Windows 下可正常工作。
- **不要使用 `opensandbox`（商业版 EE）**：它经宿主 `docker.sock` 动态创建嵌套沙箱容器，并要求「backend 容器内路径 == 宿主 daemon 视角路径」严格一致；Docker Desktop 的 daemon 运行在独立的 `docker-desktop` 发行版中，文件系统视角与用户发行版不一致，路径白名单校验与 bind-mount 大概率失败。该 provider 仅建议在 Linux 主机使用。

## 启动

全部命令在 WSL2 bash 内执行（`make`、`scripts/deploy/*.sh` 均为 bash 脚本，PowerShell/CMD 无法直接运行）：

```bash
docker compose --profile script_runner up -d --build
```

验证：

```bash
docker compose ps
curl -fsS http://localhost:3000/api/health
```

## 可选：mem0 记忆基础设施

`mem0` profile（Milvus + etcd + MinIO + Neo4j）内存占用大。Windows 下建议先在 `%UserProfile%\.wslconfig` 提高 WSL2 内存上限（如 `memory=12GB`），再启用：

```bash
docker compose --profile mem0 up -d
```

## 已知限制汇总

| 项 | 说明 |
|---|---|
| Windows 原生运行后端 | 不支持（POSIX 依赖、容器路径假设、docker.sock 依赖） |
| `opensandbox` provider（EE） | Docker Desktop 下不可用，用 `script_runner` |
| ARM Windows | 不支持（amd64-only 镜像与二进制） |
| 仓库/存储放 `/mnt/c` | 不支持（性能与路径语义问题），必须放 WSL2 文件系统 |
| 管理后台「沙盒依赖重建」 | 依赖 `DOCKER_GID` 正确配置；GID 不对时该功能优雅降级，不影响其他功能 |
