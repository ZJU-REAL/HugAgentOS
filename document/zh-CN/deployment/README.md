# 部署指南

> 最后更新：2026-07-21 ｜ [English](../../en/deployment/README.md)

HugAgentOS 支持多种部署方式，从「个人单机零依赖尝鲜」到「团队生产」再到「内网离线交付」。本页帮你**选对方式**，各方式的完整步骤见对应文档。

## 选择部署方式

| 部署方式 | 适用场景 | Docker | 数据库 | 多用户 | 文档 |
|---|---|---|---|---|---|
| **Windows 桌面一键安装** | Windows 个人用户；安装客户端时同时构建本机服务 | 不需要 | SQLite | 否（单用户） | [windows-deployment.md](windows-deployment.md) |
| **无 Docker 一键安装** | 个人单机尝鲜、二次开发体验；一条命令装好即用 | 不需要 | SQLite | 否（单用户） | [quick-install.md](quick-install.md) |
| **Docker Compose** | 团队 / 生产的标准形态，多用户、全功能 | 需要 | PostgreSQL | 是 | [docker-compose.md](docker-compose.md) |
| **离线生产部署（商业版 EE）** | 政务 / 内网等隔离环境，镜像 tarball 离线交付 | 需要 | PostgreSQL | 是 | [offline-production.md](offline-production.md) |

跨平台与参考：

| 文档 | 说明 |
|---|---|
| [Windows 部署](windows-deployment.md) | 桌面端本机服务一键安装，或以 Docker Desktop + WSL2 跑 Compose 版 |
| [环境变量参考](environment-variables.md) | 全量环境变量逐组说明（默认值 / 作用 / CE·EE 相关性） |

## 一句话对比

- **Windows 桌面一键安装**：运行 NSIS 安装包并选择“同时安装本机服务”。首次启动会创建
  独立 Python 环境、启动回环地址服务并进入登录，无需 Docker 或 WSL2。数据保存在当前用户的
  `%LOCALAPPDATA%` 下，定位同样是单进程单用户。
- **无 Docker 一键安装**：Linux/macOS 上最快的命令行路径。运行 `curl -fsSL https://raw.githubusercontent.com/ZJU-REAL/HugAgentOS/main/install.sh | bash`，安装器会创建管理员、配置模型、启动服务并打开浏览器。数据保存在 `~/.hugagent/`。**单进程单用户**，适合个人试用与开发，不适合多人或生产环境。
- **Docker Compose**：推荐的标准部署。全部服务由一个 `docker-compose.yml` 编排（PostgreSQL + Redis + 后端 + MCP + 前端 + 沙箱），支持多用户、持久沙箱、分层记忆等全部能力。
- **离线生产部署（EE）**：面向无法在线拉镜像的隔离环境，在联网侧打镜像 tarball，拷到生产侧 `docker load` + `compose up`。属商业版交付范畴。

> 能力对比、社区版 / 商业版差异见 [版本对比](../editions/overview.md)。

## 部署后验证

无论哪种方式，起服务后都可用健康检查确认后端就绪：

```bash
# 无 Docker 一键安装（默认端口 3001）
curl -fsS http://127.0.0.1:3001/api/health

# Docker Compose（默认前端端口 3002，经 nginx 反代 /api）
curl -fsS http://localhost:3002/api/health
```

返回 `{"status":"healthy",...}` 即后端正常；随后用浏览器打开对应地址、以管理员账号登录即可。
