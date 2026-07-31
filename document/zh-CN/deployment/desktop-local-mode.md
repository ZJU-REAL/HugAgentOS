# 桌面端离线本机模式

> 适用于 Windows x86_64、macOS Apple Silicon / Intel、Linux x86_64。

桌面端默认发行“完整离线包”。安装包同时携带当前平台的 CE 服务源码和私有 CPython 3.11
运行时，最终用户不需要安装 Python、uv、pip、编译器或 Docker，也不需要在首次启动时访问
PyPI。联网只用于用户主动配置的在线模型、更新和联网工具。

## 用户侧流程

1. 安装并启动桌面客户端，选择“本机模式”。
2. 客户端校验服务清单、运行时清单、平台、依赖指纹和运行时 SHA-256。
3. 客户端检查磁盘空间，把资源安全解压到暂存目录，并执行完整 Python/源码导入自检。
4. 自检成功后原子写入 `active.json`，使用私有解释器启动只监听 `127.0.0.1:32101` 的服务。
5. `/health` 通过后进入登录页面。通常只需数十秒，过程中不会安装系统软件。

安装中断不会覆盖已激活版本。升级前会备份 `data.db`、Milvus Lite 数据和本机配置；新版本
启动失败时会恢复 `previous.json` 与对应数据备份。成功升级后只保留当前/上一份运行资源和最近
三份数据备份。

## 发行目标

| 目标 | 私有运行时 | 业务数据 |
|---|---|---|
| Windows x86_64 | CPython 3.11 Windows x64 | `%LOCALAPPDATA%\com.hugagent.desktop\local-server\data` |
| macOS Apple Silicon | CPython 3.11 arm64 | `~/.hugagent` |
| macOS Intel | CPython 3.11 x86_64 | `~/.hugagent` |
| Linux x86_64 | CPython 3.11 manylinux/glibc x64 | `~/.hugagent` |

源码与运行时统一放在 Tauri 应用本地数据目录的 `local-server/releases/`（Windows 使用等价短目录
`local-server/r/`），按内容指纹复用；Windows 物理目录使用 128-bit 指纹前缀，状态清单仍保存和
校验完整 SHA-256。它们与业务数据分离，可在保留账号、对话、文件和工作区的情况下重新安装。

## 发布构建

桌面专用依赖全部位于 `desktop/`：

- `requirements-desktop.txt`：直接依赖；
- `requirements-desktop-build.txt`：仅供发布构建机隔离运行 CE 生成器；
- `requirements-desktop-macos-overrides.txt`：macOS 双架构和最低系统版本兼容 override；
- `requirements-desktop-windows-py311.lock`；
- `requirements-desktop-linux-x86_64-py311.lock`；
- `requirements-desktop-macos-aarch64-py311.lock`；
- `requirements-desktop-macos-x86_64-py311.lock`。

修改依赖后在仓库根目录执行：

```bash
npm --prefix desktop run lock:desktop
```

每个平台必须在对应系统/架构的发布构建机上生成自己的运行时和安装包：

```bash
cd desktop
npm install
npm run build       # 默认：完整离线本机包
npm run build:thin  # 可选：仅连接团队服务器的精简包
```

macOS Apple Silicon 与 Intel 必须分别生成原生安装包。完整离线运行时与 CPU 架构绑定，构建脚本
会拒绝 `universal-apple-darwin` 和跨架构 `--target`，避免 universal 外壳携带单一架构 Python。

完整构建会先编译前端与 CE 派生树，再用 `uv` 把当前平台锁同步到私有 Python，运行
`uv pip check` 和源码导入冒烟测试，最后生成确定性的 `runtime-core.tar.gz` 与 SHA-256 清单。
macOS 正式构建还会在归档前签名其中的 Mach-O 文件。Linux 运行时应在产品支持的最老 glibc
基线构建；Windows 归档和客户端解压使用扩展长度路径，不依赖系统启用 `LongPathsEnabled`；Windows
与 macOS 不支持从 Linux 交叉生成最终运行时。

精简包不携带本机运行时，界面会禁用本机/双模式，适合只连接团队服务器的受管终端。不要把精简
包标为本机离线版。

## 验证

发布前至少执行：

```bash
npm --prefix desktop run test:scripts
cargo test --manifest-path desktop/src-tauri/Cargo.toml --lib
```

在对应平台完成一次安装包冷启动，并确认日志中没有 `pip install`、`uv pip sync`、`winget` 或
Python 下载。各平台构建机还可运行仓库中的 ignored 端到端测试，它会真实解压 1 GiB 以上运行时、
启动服务并请求 `/health`。
