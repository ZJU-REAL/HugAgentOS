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
- 双端模式的工具定义以云端动态 capability manifest 为唯一真源。本机不内置或
  维护工具 schema，只缓存云端当前 revision 的完整脱敏 schema；清单只在登录和
  云端能力增删（响应头里的能力变更号）时刷新，令牌续期不触发任何对账
- 混合模式不使用按工具名称保留本机的例外名单，也不提供旧的整桥关闭环境开关；
  定时任务、批量执行、图表与建站等 MCP 统一按当前账号授权清单解析。定时任务插件
  在授权清单提供相应工具后支持用户明确选择本机或云端执行，并保留独立的任务与回执归属。
  用户配置的
  独立本机 MCP 仍按正常来源选择规则使用。
- Agent 装配不逐个跨公网探测云端 MCP。模型真正选择工具时才调用 JSON 网关，
  云端会按最新用户授权、服务状态、工具 allowlist 和 schema hash 再校验；过期
  快照不会继续执行，也不会压掉本机可用工具或阻塞模型首轮响应
- 本机只接受当前 capability manifest 协议，不包含旧 schema 形态或工具名单的
  兼容分支。已安装旧客户端继续由云端透明 MCP 端点承接，协议演进集中在云端
- 技能同样以云端为真源：本机随同一次清单刷新、同一枚 token 拉取技能清单
  （`/v1/desktop/capability/skills/manifest`，含每个技能的内容哈希），哈希变化的
  技能按需下载并保存到本机能力存储；每次运行固定所用版本，技能列表直接提供该版本的真实
  `SKILL.md` 路径。桌面执行不使用容器技能路径别名，也不在会话目录建立技能映射。
  云端目录以最高优先级注册为技能来源，同 id 的本机内置技能被云端版本覆盖；本机独有的技能保留。
  云端断线沿用上一份快照，切换账号整目录清空
- 清单回答的是「这个账号装了什么」，不是「开着什么」：技能与连接器不论云端当前
  启停一律下发，本机照样装齐——插件的子技能和智能体依赖的技能在云端常是关着的。
  云端的启停只作本机首次落地的初值，之后开关归本机，再同步一次不会翻掉用户在
  本机的选择。管理员在云端停用的能力不在下发范围内，本机也打不开
- 能力中心读的就是本机这份安装登记（`device_capability_installations`），与运行时
  解析同源：显示的即生效的。首轮同步完成前仍读云端，避免渲染一版残缺清单
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

# 可选「仅混合模式」包：只交付「本机 + 云端」双模式，首启不再让用户选运行模式
JX_DEFAULT_SERVER_BASE=https://你的后端 npm run build:hybrid-only

# 开发调试：先确保 src/frontend 已 npm run build（反代直接 serve dist），再
HUGAGENT_SERVER_BASE=https://你的后端 npm run dev
```

### 构建选项：仅交付混合模式

默认打出的包在首次启动时让用户三选一（仅本机 / 仅云端 / 本机 + 云端）。如果这批包本来就
只打算按混合模式交付，可以在构建时关掉这道选择：

```bash
JX_DEFAULT_SERVER_BASE=https://你的后端 npm run build:hybrid-only
# 等价写法（也适用于 build:thin 等其它入口）
JX_DESKTOP_HYBRID_ONLY=1 JX_DEFAULT_SERVER_BASE=https://你的后端 npm run build
```

打开后客户端行为的差别：

- 首启**不问运行模式、也不问服务器地址**，直接展示带动画的初始化页，只有一个「开始初始化」；
- 确认后**不重启应用**——运行形态在内存里已备好，同一个窗口直接切到安装进度页并自动开装；
- 云端地址固定为构建时的 `JX_DEFAULT_SERVER_BASE`。因为不再向用户询问地址，缺这个变量时
  构建脚本会**直接失败**，不会打出一个指向开发默认地址的包；
- 安装器遗留的运行模式选择一律丢弃，菜单里也不再有「更改运行模式」。

默认（不设 `JX_DESKTOP_HYBRID_ONLY`）行为完全不变，三选一初始化页照旧。

### 构建选项：本机端口命名空间

本机后端、脚本执行服务和内置 MCP 的端口默认是 `32101`、`8900`、`9100–9116`。同一台机器上要
装两个不同品牌的包时，用构建变量把其中一个整体挪走，两边就不会互相抢端口：

| 变量 | 作用 | 默认值 |
|---|---|---|
| `JX_LOCAL_SERVER_PORT` | 本机后端端口 | `32101` |
| `JX_LOCAL_SCRIPT_RUNNER_PORT` | 脚本执行服务端口 | `8900` |
| `JX_LOCAL_MCP_PORT_OFFSET` | 内置 MCP 端口整体偏移 | `0` |

壳把后两者以 `SANDBOX_RUNNER_URL` 和 `HUGAGENT_LOCAL_MCP_PORT_OFFSET` 注入本机后端进程，
后端不再写死端口。升级换过端口后，旧端口上的本机服务按记录的 PID 与安装根回收，不会误伤
同机其它产品。老 `server.json` 里记的旧端口在启动时按当前端口纠正，**不重写配置文件**。

> `brand.rs` 里的每个构建变量都登记进了 `build.rs` 的 `rerun-if-env-changed`，改了变量值重新
> 打包一定会重编译，不会复用上一次的产物。

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
`uv 0.11.33`；macOS 正式发布建议配置 Apple 证书、签名身份与 notarization 所需 secrets。

Apple 凭据不是生成测试安装包的硬前置。公开 CE 的 Release workflow 在未配置 Apple secrets 时会
自动使用 ad-hoc 身份（`-`）签名 App 及内置 Python runtime 的 Mach-O 文件，并分别生成 Apple
Silicon 与 Intel DMG；用户首次打开时需要在“系统设置 → 隐私与安全性”中选择“仍要打开”。配置
Developer ID 与 notarization secrets 后，workflow 会自动改用正式签名，避免这一步人工放行。
Tauri updater 的 `TAURI_SIGNING_PRIVATE_KEY` 是独立的更新包验签机制，仍为必需项。

## 关键文件

| 文件 | 职责 |
|---|---|
| `src-tauri/src/lib.rs` | 入口：起反代、建窗口（挂菜单栏）、deep-link、导航守卫、托盘、全局快捷键、悬浮问答窗、服务器配置窗 |
| `src-tauri/src/proxy.rs` | 本地反代：静态 serve + `/api` 转发 + cookie 注入 + SSE 透传；`/__desktop/*` 原生页（登录/关闭确认/服务器配置） |
| `src-tauri/src/auth.rs` | 系统凭据库存取、旧会话迁移、handoff 票据 redeem、会话版本隔离 |
| `src-tauri/src/credential_store.rs` | Windows Credential Manager / macOS Keychain / Linux Secret Service |
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
  macOS 使用系统菜单栏和左侧原生交通灯，主界面背景延伸至窗口顶部，右侧不再预留标题空行；交通灯的安全间距只作用于侧栏内容，
  折叠侧栏为 88px。侧栏顶部与内容标题栏空白支持拖动，独立壳页面保留 28px 安全区。
- **视图缩放**（菜单「视图 → 放大 / 缩小 / 实际大小」，或 `Ctrl` + `+` / `-` / `0`、`Ctrl`+滚轮）：
  按浏览器档位调整页面与图标大小，选定档位写入 `prefs.json`，新开窗口与下次启动沿用。系统 DPI
  缩放由 WebView 原生处理，壳层只叠加用户自己的档位；「实际大小」即完全跟随系统显示设置。
  标题栏的缩放动作走 `POST /__desktop/zoom/:mode` 投递，**不能**改用其它菜单项那套导航哨兵——
  哨兵那次「发起即取消」的导航会被 WebView2 连带把 ZoomFactor 重置回 1.0。
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

发布统一使用仓库内的 `python3 desktop/scripts/publish-desktop.py`（Python 3.8+，
Linux/WSL 或 macOS）。例如：
```bash
python3 desktop/scripts/publish-desktop.py --version 0.5.69 --target darwin-aarch64 \
  --bundle /path/to/bundle/macos --ssh user@release-host --container app-backend \
  --required-target darwin-aarch64 --required-target windows-x86_64
```
Windows 使用 `--target windows-x86_64` 和 NSIS 目录；支持原有 `--ssh-port`、
`--release-dir`、`--notes` 参数。`--local-dir` 可直接发布到本机目录；
`--dry-run` 在临时目录模拟，不修改发布端（远程模式不读取远端现有清单）。

Linux 目标（`--target linux-*`）取目录里的 `.deb`，没有则取 `.AppImage`。
若某个平台的更新签名绑定的是文件名，而发布文件名由版本与内容哈希生成，就先用
`--plan` 问出这个名字和哈希、签完再发布——`--plan` 只读产物、不改发布端，
发布文件名的生成规则因此只有这一处。

新客户端通过 `latest.json?target={{target}}&arch={{arch}}` 按平台获取可用版本；
未发布的平台返回 204。发布目录的 `release-index.json` 保存各平台版本和旧客户端共用版本，
后端仍通过公开的 `latest.json` 接口提供 Tauri 标准响应。先部署支持该索引的后端，再发布新包。
旧客户端不带平台参数，只有所有已发布及 `--required-target` 指定的平台都提供同版本包时，
共用清单才晋级。`latest.json` 文件是共用版本的兼容镜像；不要手写或手拷清单。

发布按文件锁串行处理，上传包通过 SHA-256 校验后才原子切换索引。文件名包含版本、
平台和内容哈希，已发布的同版本同平台不能替换为不同内容；修正安装包必须提升版本号。
单个平台先发布不会删除其他平台或把旧包标成新版本。旧版 Mac 若此前一直缺失平台条目，
需要先补齐同版本 Mac 和 Windows 包，共用清单晋级后即可恢复检查更新。

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


### 桌面会话与短期能力授权

会话 token 保存到系统凭据库，以安装目录和云端地址共同隔离。Windows 使用
Credential Manager，macOS 使用 Keychain，Linux 使用系统的 `secret-tool`
连接 Secret Service。系统凭据库不可用或被锁定时，本次会话仅存在于内存，
不会写入明文文件；下次启动需要重新登录。旧 `auth.json` 在读取后迁移并删除。
退出会留下不含凭据的 `auth-cleared` 标记，防止凭据库不可用时恢复旧账号。

双模式在配置目录中保存不含凭据的稳定 `device-id`，在设备能力令牌（有效期
10 分钟）到期前一分钟续签，并将设备身份带到能力请求。续签只更新令牌；模型拓扑
带 revision，云端回 304 时本机模型配置不被触碰。启动断网或同步失败时自动重试。
退出会清除本机身份、能力桥并注销云端会话；旧登录和同步请求的迟到响应按会话版本丢弃。

开发环境运行 Rust 单测可暂时覆盖发布资源清单（无需生成离线运行时包）：

```bash
cd desktop/src-tauri
TAURI_CONFIG='{"bundle":{"resources":[]}}' cargo test --lib --offline
```

此检查不覆盖 Windows/macOS 系统凭据库的实机访问，也不执行标记为 ignored 的完整运行时解包测试。

### Windows 卸载文件安全回归

NSIS 默认保留业务数据和顶层 `skills`、`plugins`、`agents`、`mcp.json`、`.capabilities`；仅用户明确选择删除时清理这些目录。卸载前拒绝重定向的应用根和运行目录，清理助手只枚举普通目录，对 junction/reparse point 仅删除链接本身。运行目录仍先原子移走，再由隐藏后台助手清理。

可在 Windows 的独立 TEMP 夹具中运行 `scripts/windows-uninstall-fixture.ps1`，传入新建且名称以 `codex-cap-uninstall-` 开头的 TEMP 直属目录和 `src-tauri/uninstall-cleanup.ps1` 的绝对路径。夹具仅操作其指定目录，覆盖保留/显式删除、联接目标保护、长路径及恢复失败。该检查不运行产品安装器，也不能替代完整的新装、升级、静默更新和交互卸载验收。

### OfficeCLI 随包交付与预览认证

完整桌面依赖包包含 OfficeCLI 1.0.144 原生可执行文件（版本与当前沙箱镜像一致），
由 `native-tools.json` 按五个支持的平台/架构锁定下载地址、大小与 SHA-256。
构建机下载并校验后，将它放到私有 Python 的可执行目录；本机脚本执行服务的清洁 PATH
已包含该目录，因此无需在用户电脑上联网安装或修改系统 PATH。随包保留上游许可证。
构建与安装自检均运行 `officecli --version`，缺失或版本不符直接失败。

依赖指纹覆盖 Python 锁文件、原生工具清单、运行时构建/归档/自检脚本和许可证。
修改这些输入后，下次构建及客户端安装不会复用旧运行时；只改业务源码仍可复用未变更的依赖。
完整包同时携带 Pandoc 3.11 与 LibreOffice 26.2.6。Pandoc 位于私有 Python 可执行目录，
LibreOffice 的完整程序、字体和资源位于运行时的 native/libreoffice；后端优先按运行时清单
查找它们。用户安装客户端时离线解压这些工具，无需另装 Pandoc 或 LibreOffice。
截图仍需要 Chromium。

发布构建机按当前原生平台提取官方 ZIP/TAR、DMG 或 MSI；Windows 使用 MSI 管理映像提取，
Linux 使用 dpkg-deb（仅提取文件），macOS 使用 hdiutil/ditto。不向构建机或用户系统安装
LibreOffice，不注册文件关联。五个平台资产均锁定大小与 SHA-256，并保留许可证及源码链接。
macOS 对嵌套 LibreOffice.app 重新签名并验证封装。工具更新会改变依赖指纹。
构建和安装自检实际执行 Markdown→DOCX→文本及 DOCX→PDF；缺失、版本不符或转换失败即阻止
运行时激活。完整包体积与解压空间会增加。精简包不携带这些工具。
跨架构发行仍须在对应原生构建机完成验证；Linux 构建机需具备 dpkg-deb 及 LibreOffice 所需系统库。

本机文件预览与路径查询均识别桌面桥接身份，访问原文件时保留项目和目录权限检查。
本机预览的 401 不会触发云端退出登录；云端会话过期保持原有登录流程。
HTML 加载失败显示可读提示，正常 HTML 在不允许同源访问的脚本沙箱中预览。

桌面托管的 OfficeCLI 通过 `OFFICECLI_SKIP_UPDATE=1` 关闭后台自更新，版本随客户端依赖包升级。

### 桌面菜单与下载更新

混合模式的“文件”菜单提供“新建对话”“打开文件夹”“退出”。选择文件夹后，会进入绑定该本机项目的新对话；取消选择不改变当前对话。

“检查更新”和“关于”显示当前桌面版本。启动时会在后台检查新版；有更新时，侧栏帮助图标变为下载按钮。点击后可查看当前版本、目标版本及更新说明，确认后显示下载进度卡片，完成后自动重启。取消或失败可以重试。Windows 更新采用静默安装，不展示安装器界面；开始替换程序时客户端和进度卡片会关闭，再自动打开新版。系统要求的权限提示仍由操作系统决定。

这些交互随新版桌面包交付，旧客户端需要先升级一次才能使用。

桌面新版发现采用持续发布通知。更新服务器每个工作进程共享一个版本清单观察任务，约每 2 秒检查一次文件元数据；清单变化时通知在线客户端，客户端再检查对应平台版本。侧栏复用本机状态事件连接，取消每 2 秒请求状态，相同状态不重复渲染。没有版本变化时只发送约 15 秒一次的连接心跳，另保留约 30 分钟一次的版本校验。

连接中断会退避重连；旧服务器不支持通知时约每 5 分钟检查一次。新旧客户端均保留手动“检查更新”。快速通知需要同时升级发布服务器后端和桌面包；仅升级客户端会使用低频兼容检查。发现新版仍只提示，不会自动下载安装。

更新进度窗通过客户端既有回环服务加载内嵌页面，避免 Windows 对 data: 页面的兼容性限制。Windows 发布前应在原生构建机完成正式构建后、在交互式桌面会话运行 powershell -ExecutionPolicy Bypass -File desktop/scripts/test-update-progress.ps1，验证真实 WebView2 窗口及进度渲染；此测试不下载或安装更新。


### Windows 私有 Bash

Windows 完整安装包携带来自 Git for Windows 2.55.0.5 的私有 Bash、MSYS 运行库和
GNU 文件命令。构建机下载固定资产并校验大小及 SHA-256，用户安装时离线解压到
运行时的 `native/git-bash`，不需要另装 Git、WSL，也不修改系统 PATH。
运行器直接调用私有 `usr/bin/bash.exe`；随包运行时损坏时明确失败，不回退到系统 Git。

构建和安装激活前，使用不含系统 Git 的 PATH 实际运行 Bash，检查中文及带空格目录、
`find/sort/head/cut` 管道、`grep` 搜索与常用文件命令；自检失败阻止该运行时激活。
新增依赖会改变运行时指纹，升级时不会复用旧的缺少 Bash 的依赖包。
非 Windows 平台沿用系统 Bash，精简云端包不携带此运行时。

此修复需要重新构建并分发 Windows 完整客户端；只更新云端后端不能修复已安装的旧包。


原生 Windows 构建机可运行独立验收（输出目录必须尚不存在，保留结果供核验）：

```powershell
python desktop/scripts/verify-windows-bash.py --archive <已下载的固定版本Git归档> --output <新的临时目录>
```

验收使用原有 SHA-256 校验，实际经过打包、搬移、解包后再执行自检；随后临时移走
`grep.exe`，确认自检拒绝缺失依赖，再恢复文件。运行器与自检共享 GNU 命令优先于
Windows System32 的 PATH 顺序。


### 多窗口

Windows、macOS、Linux 的“文件 → 新建窗口”打开一个独立桌面窗口。
快捷键为 Windows/Linux 的 Ctrl+Shift+N、macOS 的 Cmd+Shift+N；UOS Electron 同样支持。
各窗口共享登录、服务器配置和本机服务，菜单、窗口控制及文件夹选择作用于发起窗口。
多个普通窗口可见时关闭一个不会退出客户端；最后一个可见窗口继续遵循关闭到托盘/退出偏好
（macOS 关闭窗口后保留应用）。菜单“退出”仍退出整个应用。


### 对话工作目录

未绑定本地项目时，每个对话使用 `<数据目录>/workspace/.sessions/<会话哈希>/`。
同一对话重复运行使用同一目录，不因空闲自动删除；不同对话使用不同目录。
Bash 的当前目录与文件工具的相对路径基准一致，绝对路径直接指向真实文件，不进行别名改写。
绑定本地项目时，环境上下文用 `project_root` 单独提供项目真实路径；`cwd` 仍为会话目录。
项目命令需要在同一次 Bash 调用中显式 `cd` 到带引号的项目路径，文件工具使用项目绝对路径。
桌面普通会话、自定义模式和子智能体均获得 `environment_context`（cwd、OS、shell、日期、时区与权限快照）。
Windows 当前执行器为随包 Git Bash，因此声明 `shell=bash`，不声明 PowerShell；云端不注入此环境块。

每次命令的执行脚本使用独立临时文件，命令结束仅清理自己的脚本，避免并发覆盖和删除用户同名文件。
技能说明使用当前工作目录内的相对路径；技能自身脚本通过当前技能真实路径启动，不切换到技能目录写产物。
包含空格、中文或 Windows 盘符的路径由命令参数引用处理，不依赖工作区映射。
桌面与容器的路径、技能目录和 runner 工作区规则分别实现；容器继续使用自己的挂载与 My Space 约定。

### macOS 更新归档校验

手动生成 .app.tar.gz 时使用 python3 desktop/scripts/create-macos-update.py --app /path/Example.app --output /path/Example.app.tar.gz --version X.Y.Z。
不要直接用 macOS 默认 tar 生成更新归档，它可能自动加入 ._* AppleDouble 元数据，导致 Tauri 更新解包失败。
发布工具会拒绝 AppleDouble 条目、多个应用根目录、路径穿越和与发布版本不一致的 Info.plist。生成归档后再签名，签名后不得改变内容。
