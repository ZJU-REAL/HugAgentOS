# HugAgentOS 桌面客户端（Tauri v2）

把现有 Web 平台封装为桌面客户端（Windows / macOS / Linux）。客户端支持两种运行方式：
连接已部署的团队服务器，或在 Windows、macOS 和 Linux 上由客户端离线安装并托管本机 CE 单机服务。
两种方式都通过内置本地反代访问后端。

登录走**方案 B**——系统浏览器跳转登录 + `hugagent://` deep-link 唤起 App + 一次性
handoff 票据换 token。前端源码零改动（复用 `src/frontend`）。

> 完整设计见 `internal design docs`。本目录是方案 B 的落地实现。

## 架构一图

远程服务器模式保持原有瘦客户端架构：

```
桌面App ──系统浏览器──► <server>/?desktop=1 ──SSO登录──► 前端换 handoff 票据
                                                            │
   hugagent://auth/callback?ticket=<handoff>  ◄──浏览器跳转──┘
        │ OS 唤起 App
        ▼
   POST <server>/api/v1/auth/desktop/redeem {ticket}  → 真正 session token（存 OS 私有目录）
        │
        ▼
   本地反代(127.0.0.1:随机端口)  每个 /api 请求注入 Cookie: jx_session=<token>
        │  静态资源直接 serve 前端 dist；/api/* 转发后端；SSE 逐帧透传
        ▼
   Nginx → FastAPI 后端集群（零改动）
```

deep-link 上只走**单次、秒级过期**的 handoff 票据，长期 token 永不进 URL。

三平台本机服务模式在这条链路前增加一层客户端托管：

```text
首次启动选择“本机模式”
  → 校验安装包内 server-ce.zip 与 runtime-core.tar.gz
  → 原子解压同版本 CE 源码和私有 CPython 3.11 运行时
  → 私有 python src/backend/cli.py serve --host 127.0.0.1 --port 32101
  → 健康检查通过
  → 桌面本地反代继续复用既有登录与 API 转发链路
```

本机服务不需要 Docker、PostgreSQL 或 Redis。它使用 SQLite、进程内 Redis 和宿主子进程
沙箱，定位是个人单机使用，不替代团队生产部署。

## 依赖的后端能力（后端已内置）

- `POST /v1/auth/desktop/handoff` — 浏览器侧用当前 cookie 会话换一次性 handoff 票据
- `POST /v1/auth/desktop/redeem`  — App 侧用票据换回 session token
- 前端 `?desktop=1` 桥接逻辑在 `stores/authStore.ts`

## 前置环境（构建机）

Tauri 不支持交叉编译——每个平台的包必须在对应系统上构建：Windows 包在装好工具链的
Windows 机器上打，**Linux 包可在任意装好 Rust 的 Linux / WSL 环境构建**，Mac 包需 macOS 构建机。

- **Rust** ≥ 1.77（`rustup`）
- **Node** ≥ 20（构建前端 dist）
- **Python** ≥ 3.11（生成单文件 `server-ce.zip`）
- **uv**（只在发布构建机解析并安装四个平台的锁定 Python 运行时）
- 平台依赖：Windows 装 WebView2 Runtime（Win11 自带）；Linux（Ubuntu 24.04 实测）：
  ```bash
  sudo apt install -y libwebkit2gtk-4.1-dev libgtk-3-dev libayatana-appindicator3-dev \
    librsvg2-dev libxdo-dev libssl-dev patchelf file
  ```
- 图标已生成并入库（`src-tauri/icons/`，含 Win `.ico` / Linux PNG / Mac `.icns`）；换品牌 logo
  时重新生成：见 `src-tauri/icons/README.md`

## 配置服务器地址

运行时配置文件 `<应用配置目录>/server.json`（不入库）：

```json
{
  "deployment_mode": "remote",
  "server_base": "https://agent.example.gov.cn",
  "cookie_name": "jx_session",
  "insecure_tls": false
}
```

- `<应用配置目录>`：Windows `%APPDATA%\com.hugagent.desktop`，macOS
  `~/Library/Application Support/com.hugagent.desktop`，Linux `~/.config/com.hugagent.desktop`
- `deployment_mode` 可取 `remote` / `local`；切换本机服务时客户端会把地址固定为
  `http://127.0.0.1:32101`
- 也可用环境变量 `HUGAGENT_SERVER_BASE` 覆盖（优先级高于 server.json，并强制切回远程模式）
- `cookie_name` 必须与后端 `SESSION_COOKIE_NAME` 一致（默认 `jx_session`）
- 内网自签 HTTPS 时把 `insecure_tls` 设为 `true`
- 编译期默认值来自 `src-tauri/src/brand.rs`，可用 `JX_DEFAULT_SERVER_BASE` 覆盖；正式分发务必通过构建变量、server.json 或环境变量配置实际服务地址
- 本机模式的桌面更新源用 `JX_DESKTOP_UPDATE_BASE` 在构建时指定（未设则回退
  `JX_DEFAULT_SERVER_BASE`），也可由 `HUGAGENT_UPDATE_SERVER_BASE` 在运行时覆盖

## 构建 / 运行

```bash
cd desktop
npm install            # 装 @tauri-apps/cli

# 默认生产构建：完整离线包，包含 CE 源码和当前平台私有 Python 运行时
# Windows 侧 → 打 NSIS .exe：      产物 src-tauri/target/release/bundle/nsis/
# macOS Apple Silicon / Intel 构建机分别打原生 DMG；不要生成 universal 包
# Linux 本机 → 打 AppImage + deb：产物 src-tauri/target/release/bundle/{appimage,deb}/
npm run build

# 可选云端精简包：不提供本机模式，不携带约 350 MiB 的压缩运行时
npm run build:thin

# 开发调试：先确保 src/frontend 已 npm run build（反代直接 serve dist），再
HUGAGENT_SERVER_BASE=https://你的后端 npm run dev
```

> 平台打包目标由 `src-tauri/tauri.linux.conf.json`（Linux：AppImage + deb）、
> `src-tauri/tauri.windows.conf.json`（Windows：NSIS + CE 本机服务）和
> `src-tauri/tauri.macos.conf.json`（macOS：app + DMG + CE 本机服务）覆盖基础配置。
> Linux 只有 **AppImage 支持自动更新**，deb 仅作首装分发。WSL 下打 AppImage 建议带
> `APPIMAGE_EXTRACT_AND_RUN=1`。
> 完整离线包的 Python 运行时与 CPU 架构绑定；构建脚本会拒绝 macOS universal 或跨架构
> `--target`。Apple Silicon 与 Intel 必须在对应架构构建并分别发布。

> 三个平台 overlay 的 `beforeBuildCommand` 都会运行 `scripts/prepare-bundle.mjs`：构建
> 桌面前端、准备 CE 服务树、构建 CE 登录前端、删除构建期 `node_modules`，再把全部服务文件压成
> 单个 `server-ce.zip` 后交给 Tauri 打包。源代码仓存在
> `scripts/build_ce.py` 时，脚本正常运行生成器并执行开源边界门禁；公开 CE 仓不含生成器，脚本会先
> 校验根目录 `.hugagent-edition` 为 `ce`，再只复制当前已派生 checkout 中的 Git tracked 文件。
> 同时生成与当前系统/架构匹配的 `runtime-core.tar.gz`。dev 模式从仓库内
> `src/frontend/dist` 读取静态资源；终端用户安装或首次启动时不会运行这些构建步骤。

### 跨平台本机服务依赖

本机模式使用独立的 Python 3.11 依赖档案，避免把容器部署专用的
PostgreSQL、云存储和远程沙箱 SDK 安装到最终用户环境：

- `desktop/requirements-desktop.txt` 只声明 SQLite、local storage 和 host script runner
  所需的直接依赖。
- `desktop/requirements-desktop-build.txt` 锁定 CE 归档生成器所需的发布构建依赖；
  由 `uv run` 隔离使用，不进入用户运行时。
- `desktop/requirements-desktop-macos-overrides.txt` 固定两种 Mac 架构与最低系统版本都可用的
  特殊 wheel 版本。
- `desktop/requirements-desktop-{windows,linux,macos-*}-py311.lock` 分别锁定 Windows x86_64、
  Linux x86_64、macOS Intel 和 Apple Silicon 的完整传递依赖。`uv pip sync` 只在发布构建机执行；
  用户机器只校验并解压成品运行时。
- `desktop-bundle.json` 的 `dependency_fingerprint` 标识依赖内容。客户端更新只
  修改源码或前端、且指纹不变时，会复用现有私有 Python 运行时。

修改桌面依赖后，必须在仓库根目录重新生成并提交锁文件：

```bash
npm --prefix desktop run lock:desktop
```

构建脚本会校验锁文件内的输入 SHA-256；锁文件过期时会在耗时的前端构建前失败。
锁生成与运行时构建需要网络；发布后的完整安装包不需要 PyPI、Python、uv 或编译器。

正式发版前需确保工作区干净，并在 Windows PowerShell 设置
`$env:HUGAGENT_RELEASE_BUILD="1"`；此时 CE 生成器不会接受 `--allow-dirty`。版本号必须同时更新
`package.json`、`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml`（本机服务从 `0.2.0` 起提供），
`prepare-bundle.mjs` 会在耗时构建开始前校验三者一致。公开 CE 的 Desktop Release workflow 会在
启动 Windows x86_64、Linux x86_64、macOS arm64、macOS x86_64 四个原生目标前校验 release tag
必须精确等于 `desktop-v<上述版本号>`；版本或 tag 不一致时不会创建任何平台产物。工作流固定使用
`uv 0.11.33`；macOS 正式发布还需配置 Apple 证书、签名身份与 notarization 所需 secrets。

## 关键文件

| 文件 | 职责 |
|---|---|
| `src-tauri/src/lib.rs` | 入口：起反代、建窗口（挂菜单栏）、deep-link、导航守卫、托盘、全局快捷键、悬浮问答窗、服务器配置窗 |
| `src-tauri/src/proxy.rs` | 本地反代：静态 serve + `/api` 转发 + cookie 注入 + SSE 透传；`/__desktop/*` 原生页（登录/关闭确认/服务器配置） |
| `src-tauri/src/auth.rs` | token 落盘 + handoff 票据 redeem |
| `src-tauri/src/config.rs` | server.json / 环境变量 / 默认值；`save_server_base` 写回 |
| `src-tauri/src/local_server.rs` | 本机服务安装、版本检测、进程托管、健康检查、进度与日志状态 |
| `src-tauri/src/local_payload.rs` | 三平台离线归档校验、安全解压、内容寻址、原子激活与自动回滚 |
| `src-tauri/src/menu.rs` | 平台菜单构建 + 事件分发；macOS 使用系统菜单栏，Windows/Linux 使用窗口内菜单 |
| `src-tauri/src/notify.rs` | **A1** 后台通知轮询 → 原生系统通知（接后端 `automations/notifications/list`） |
| `src-tauri/src/update.rs` | **A3** 一键自动更新：拉后端 manifest → 验签 → 安装 → 重启 |
| `src-tauri/tauri.conf.json` | 窗口/打包/deep-link scheme/资源/**updater 配置（pubkey + endpoints）** |
| `src-tauri/installer-hooks.nsh` | Windows 首装模式选择；卸载时停止服务、保留或删除数据，并异步清理运行环境 |
| `scripts/prepare-bundle.mjs` | 发行构建前生成同版本 CE 服务资源、清单和离线运行时 |
| `scripts/build-runtime.mjs` | 用平台锁构建、检查、冒烟测试并归档可迁移的私有 CPython 运行时 |
| `scripts/create-ce-archive.py` | 以稳定顺序把 CE 服务树压缩成 `server-ce.zip` |
| `requirements-desktop.txt` | 桌面本机服务专用的跨平台直接依赖档案 |
| `requirements-desktop-build.txt` | 桌面发布构建机专用的精确 Python 依赖 |
| `requirements-desktop-macos-overrides.txt` | macOS 双架构兼容性 override |
| `requirements-desktop-*-py311.lock` | 四个发行目标的 CPython 3.11 精确依赖锁 |
| `scripts/generate-platform-requirements-locks.mjs` | 重新生成并标记所有平台锁文件 |
| `scripts/ce-payload.mjs` | 在派生 CE 仓校验版本标识并只暂存 tracked tree，源代码仓仍走生成器 |
| `scripts/validate-release-version.mjs` | CI 三平台矩阵启动前校验桌面版本文件与 release tag |
| `src-tauri/capabilities/default.json` | 插件权限（opener / deep-link / notification / global-shortcut / updater） |

## 本次新增能力（Tier A + 菜单栏 + 一键更新）

- **A1 原生通知**（`notify.rs`）：登录后每 25s 轮询后端**已有的**通知列表
  （`/v1/automations/notifications/list`，由 `automation_scheduler` 写 Redis），对**客户端启动后
  新增**的通知发系统原生通知——托盘常驻的后台自动化任务跑完终于会主动提醒。零后端改动。
- **A2 悬浮快速问答**（`lib.rs::toggle_quickask` + 前端 `?quickask=1`）：全局快捷键
  **Ctrl+Shift+Space** 唤起/收起一个置顶小窗，加载主前端的紧凑模式（隐藏侧栏/顶栏，复用
  `chatStream.ts` 全部对话能力，零重复实现）。未登录时退化为唤起主窗。
- **A3 一键自动更新**（`update.rs`）：菜单「帮助 → 检查更新…」或托盘触发，见下方《自动更新》。
- **A4 托盘增强**：托盘菜单新增「新建对话」「检查更新…」。
- **平台化菜单与标题栏**（`menu.rs` + `proxy.rs`）：Windows/Linux 保留紧凑的一体化窗口菜单；
  macOS 使用系统菜单栏和左侧原生交通灯，窗口内只保留 38px 可拖动标题区，不再重复展示品牌栏
  或右侧操作按钮。
- **设置服务器地址 UI**（菜单「文件 → 设置服务器地址…」）：填后端地址→写回 server.json→重启生效，
  不再必须手改 JSON。
- **本机服务一键安装**（菜单「文件 → 本机服务…」）：Windows、macOS 和 Linux 后端不可达时也会自动
  显示。安装过程完全离线，并提供
  阶段进度、实时日志、失败重试和健康检查；客户端更新携带新 CE 资源时自动升级服务代码，
  业务数据保持不变；升级前自动保留最多三份关键数据库备份，启动失败会回滚源码、运行时和数据。
  远程模式下点击安装会先在当前页面完成安装，服务通过健康检查后才切换
  `server.json` 并重启，避免提前重启造成按钮无响应或安装状态不可见。

> 交互全部走「原生菜单/托盘 → Rust」或「导航到 `/__desktop/*` 哨兵 → 导航守卫」，**不依赖 Tauri
> IPC**——因为前端跑在本地反代这个「远程源」上，`window.__TAURI__`/invoke 不保证注入。这是本壳
> 一贯的可靠模式。

## 自动更新（解决「前端一改就要重编译分发」）

桌面端把前端 dist **打进安装包**，所以以往前端/壳一更新就得重新编译、重新分发客户端。现在客户端能
**整包自更新**：检查更新 → 后端拉清单 → 本地验签 → 下载安装 → 重启（新的前端 dist 一并换掉）。

**链路**：`客户端「检查更新」→ <update_base>/api/v1/desktop/latest.json →（有新版）下载安装包 →
pubkey 验签 → 安装 → 重启`。远程模式默认让更新源跟随当前 `server_base`；本机模式改用构建时
`JX_DESKTOP_UPDATE_BASE`（未设时回退 `JX_DEFAULT_SERVER_BASE`），避免向本机 CE 服务查询并不存在的
桌面安装包。后端接口见 `src/backend/api/routes/v1/desktop.py`。

### 一次性前置：生成签名密钥（不做则构建/更新都不可用）

`bundle.createUpdaterArtifacts` 已开启，**构建时必须提供签名私钥**，否则 `npm run build` 失败。

```bash
# 1. 生成密钥对（私钥务必保密、离线保管；公钥要填进 tauri.conf.json）
npx @tauri-apps/cli signer generate -w ~/.tauri/hugagent-updater.key
# 输出里的 public key 填到 tauri.conf.json → plugins.updater.pubkey
#   （占位符 REPLACE_WITH_TAURI_SIGNER_PUBLIC_KEY 必须替换）

# 2. 构建时注入私钥（Windows PowerShell 同理设环境变量）
export TAURI_SIGNING_PRIVATE_KEY="$(cat ~/.tauri/hugagent-updater.key)"
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD=""   # 生成时设了口令就填这里
npm run build
# updater 产物随平台：Windows 多出 *.nsis.zip + .sig；Linux 的 AppImage 本体即更新包，旁边出 .sig
```

> 同一对密钥三平台共用——Windows / Linux / Mac 构建都注入同一个私钥，客户端用同一个 pubkey 验签。

> `tauri.conf.json → plugins.updater.endpoints` 里的默认 endpoint 仅作占位/兜底，实际运行时会被
> Rust 侧 endpoint 覆盖；正式本机版构建须设置 `JX_DESKTOP_UPDATE_BASE` 或
> `JX_DEFAULT_SERVER_BASE` 为可发布桌面包的后端地址。

### 发布一个新版本（在后端侧）

后端从 `DESKTOP_RELEASE_DIR`（默认 `/app/desktop_release`）读取发布产物。发一版只需把三样放进去：

```
<DESKTOP_RELEASE_DIR>/
  ├─ latest.json                              # 更新清单（见下）
  ├─ HugAgentOS_0.2.0_x64-setup.nsis.zip        # 构建产物（updater 安装包）
  └─ HugAgentOS_0.2.0_x64-setup.nsis.zip.sig    # 对应签名
```

`latest.json`（`platforms.*.url` 可写**裸文件名**，后端按请求来源自动改写成绝对下载地址、并把 `.sig`
文件名内联成签名内容——一份清单通吃多环境）：

```json
{
  "version": "0.2.0",
  "notes": "本次更新说明",
  "pub_date": "2026-07-16T00:00:00Z",
  "platforms": {
    "windows-x86_64": {
      "signature": "HugAgentOS_0.2.0_x64-setup.nsis.zip.sig",
      "url": "HugAgentOS_0.2.0_x64-setup.nsis.zip"
    },
    "linux-x86_64": {
      "signature": "HugAgentOS_0.2.0_amd64.AppImage.sig",
      "url": "HugAgentOS_0.2.0_amd64.AppImage"
    }
  }
}
```

多平台共用这一份清单。**不要手写/手拷 latest.json**——用 `deploy_kit/publish_desktop.sh` 逐平台
发布（`--target windows-x86_64` / `--target linux-x86_64`），脚本会读目标环境现有清单做平台条目
合并：同版本逐平台发布互不覆盖；版本不同则丢弃其它平台旧条目并告警（旧条目会让那个平台反复
"更新"到旧包，所以发新版要把所有在用平台都发一遍）。

`latest.json` 与安装包分发接口**公开无鉴权**（Tauri updater 不带 cookie），且已在 license_gate 放行
（过期客户端也能拉到修复版）。发布目录不存在/无清单时 `/latest.json` 返回 204，客户端视为「无更新」。

## 已知注意点

- **Windows 包**仍需在 Windows 侧构建（Tauri 不支持交叉编译）；**Linux 包在装好 Rust 的
  Linux / WSL 环境可直接构建**（apt 依赖见上）。
- 默认 `npm run build` 生成完整离线包：携带 `server-ce.zip`、`runtime-core.tar.gz` 和各自清单。
  首次启动只做 SHA-256/平台/依赖指纹校验、安全解压、运行时自检和健康检查，不访问 PyPI，
  不探测或修改系统 Python。`npm run build:thin` 生成仅连接团队服务器的精简包，本机模式会明确禁用。
- Windows 的源码和运行时位于 `%LOCALAPPDATA%\com.hugagent.desktop\local-server\r`，物理目录
  使用完整 SHA-256 的 128-bit 前缀以避开 Win32 长路径限制；`active.json` 仍保存并校验完整指纹。
  业务数据仍在同一 `local-server\data`。交互卸载会询问是否同时删除数据并默认选择“否”；静默自动更新始终
  保留数据。软件分发系统可向卸载器传入 `/HUGAGENT_DELETE_DATA` 明确请求删除数据。
- macOS 的持久数据位于 `~/.hugagent`；源码、私有 Python 和版本目录位于
  `~/Library/Application Support/com.hugagent.desktop/local-server`。从旧版升级时，如果
  `~/.hugagent` 不存在或为空，客户端会把旧 `local-server/data` 原子迁入该目录；如果目录已有
  命令行版数据，则直接沿用并保留旧目录作为备份，不覆盖任何文件。macOS 把 App 拖入废纸篓不会执行卸载钩子，
  因此默认不会删除 `~/.hugagent` 或 Application Support 下的运行环境；确认不再需要后可分别
  手动删除。
- Linux 完整包与 macOS 一样把持久数据统一放在 `~/.hugagent`，运行版本放在 Tauri 应用本地数据目录；
  AppImage 与 deb 都支持本机模式。Linux x86_64 运行时应在兼容基线系统上构建，避免引入过新的 glibc。
- Linux 托盘依赖 libayatana-appindicator；Wayland 下全局快捷键（Ctrl+Shift+Space）兼容性因桌面
  环境而异。
- 依赖版本号（tauri 插件、axum/reqwest 等）以实际 `cargo build` 为准；个别 capability
  permission 标识符若构建报错，按报错提示微调 `capabilities/default.json`。
