# 部署指南

> 最后更新：2026-07-21 ｜ [English](../../en/deployment/README.md)

HugAgentOS 支持多种部署方式，从「个人单机零依赖尝鲜」到「团队生产」再到「内网离线交付」。本页帮你**选对方式**，各方式的完整步骤见对应文档。

## 选择部署方式

| 部署方式 | 适用场景 | Docker | 数据库 | 多用户 | 文档 |
|---|---|---|---|---|---|
| **桌面端离线本机模式** | Windows / macOS / Linux 个人用户；安装后直接使用 | 不需要 | SQLite | 否（单用户） | [desktop-local-mode.md](desktop-local-mode.md) |
| **UOS 1070 ARM64 桌面端** | 统信 UOS 1070 aarch64；Electron 43 `.deb` | 不需要 | SQLite / 远程 | 本机单用户 / 云端多用户 | [uos1070-desktop.md](uos1070-desktop.md) |
| **无 Docker 一键安装** | 个人单机尝鲜、二次开发体验；一条命令装好即用 | 不需要 | SQLite | 否（单用户） | [quick-install.md](quick-install.md) |
| **Docker Compose** | 团队 / 生产的标准形态，多用户、全功能 | 需要 | PostgreSQL | 是 | [docker-compose.md](docker-compose.md) |
| **离线生产部署（商业版 EE）** | 政务 / 内网等隔离环境，镜像 tarball 离线交付 | 需要 | PostgreSQL | 是 | [offline-production.md](offline-production.md) |

跨平台与参考：

| 文档 | 说明 |
|---|---|
| [Windows 部署](windows-deployment.md) | 桌面端本机服务一键安装，或以 Docker Desktop + WSL2 跑 Compose 版 |
| [桌面端离线本机模式](desktop-local-mode.md) | 三平台随包私有 Python、原子升级回滚与完整/精简包 |
| [UOS 1070 ARM64 桌面端](uos1070-desktop.md) | Electron 43、glibc 2.28、ARM64 `.deb` 构建与验收 |
| [环境变量参考](environment-variables.md) | 全量环境变量逐组说明（默认值 / 作用 / CE·EE 相关性） |

## 一句话对比

- **桌面端离线本机模式**：运行对应平台的完整安装包并选择本机模式。首次启动直接校验并解压
  随包私有 Python 和锁定依赖，启动回环地址服务并进入登录；无需 Docker、系统 Python 或首次
  联网构建。定位是单进程单用户。
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
