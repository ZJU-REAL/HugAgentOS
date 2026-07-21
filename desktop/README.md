# HugAgentOS 桌面客户端（Tauri v2）

把现有 Web 平台封装为桌面客户端（Windows / macOS / Linux）。客户端支持两种运行方式：
连接已部署的团队服务器，或在 Windows 和 macOS 上由客户端从零安装并托管本机 CE 单机服务。
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

Windows 和 macOS 本机服务模式在这条链路前增加一层客户端托管：

```text
首次启动选择“从零开始安装”
  → 安装包内同版本 CE 派生树
  → 应用本地数据目录下的 local-server/venv
  → hugagent serve --host 127.0.0.1 --port 32101
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

# 生产构建（自动先 build 前端；构建须注入更新签名私钥，见《自动更新》）
# Windows 侧 → 打 NSIS .exe：      产物 src-tauri/target/release/bundle/nsis/
# macOS 侧 → 打 universal DMG：    npm run build -- --target universal-apple-darwin
# Linux 本机 → 打 AppImage + deb：产物 src-tauri/target/release/bundle/{appimage,deb}/
npm run build

# 开发调试：先确保 src/frontend 已 npm run build（反代直接 serve dist），再
HUGAGENT_SERVER_BASE=https://你的后端 npm run dev
```

> 平台打包目标由 `src-tauri/tauri.linux.conf.json`（Linux：AppImage + deb）、
> `src-tauri/tauri.windows.conf.json`（Windows：NSIS + CE 本机服务）和
> `src-tauri/tauri.macos.conf.json`（macOS：app + DMG + CE 本机服务）覆盖基础配置。
> Linux 只有 **AppImage 支持自动更新**，deb 仅作首装分发。WSL 下打 AppImage 建议带
> `APPIMAGE_EXTRACT_AND_RUN=1`。

> Windows 和 macOS overlay 的 `beforeBuildCommand` 会运行 `scripts/prepare-bundle.mjs`：构建
> 桌面前端、准备 CE 服务树、构建 CE 登录前端，并删除构建期 `node_modules` 后再交给 Tauri
> 打包。FULL 主仓存在
> `scripts/build_ce.py` 时，脚本正常运行生成器并执行开源边界门禁；公开 CE 仓不含生成器，脚本会先
> 校验根目录 `.hugagent-edition` 为 `ce`，再只复制当前已派生 checkout 中的 Git tracked 文件。
> Linux 仍只构建桌面前端，不携带 Windows 本机服务载荷；dev 模式从仓库内
> `src/frontend/dist` 读取静态资源。

正式发版前需确保工作区干净，并在 Windows PowerShell 设置
`$env:HUGAGENT_RELEASE_BUILD="1"`；此时 CE 生成器不会接受 `--allow-dirty`。版本号必须同时更新
`package.json`、`src-tauri/tauri.conf.json`、`src-tauri/Cargo.toml`（本机服务从 `0.2.0` 起提供），
`prepare-bundle.mjs` 会在耗时构建开始前校验三者一致。公开 CE 的 Desktop Release workflow 还会在
启动三平台矩阵前校验 release tag 必须精确等于 `desktop-v<上述版本号>`；版本或 tag 不一致时不会
创建任何平台产物。

## 关键文件

| 文件 | 职责 |
|---|---|
| `src-tauri/src/lib.rs` | 入口：起反代、建窗口（挂菜单栏）、deep-link、导航守卫、托盘、全局快捷键、悬浮问答窗、服务器配置窗 |
| `src-tauri/src/proxy.rs` | 本地反代：静态 serve + `/api` 转发 + cookie 注入 + SSE 透传；`/__desktop/*` 原生页（登录/关闭确认/服务器配置） |
| `src-tauri/src/auth.rs` | token 落盘 + handoff 票据 redeem |
| `src-tauri/src/config.rs` | server.json / 环境变量 / 默认值；`save_server_base` 写回 |
| `src-tauri/src/local_server.rs` | 本机服务安装、版本检测、进程托管、健康检查、进度与日志状态 |
| `src-tauri/src/menu.rs` | 平台菜单构建 + 事件分发；macOS 使用系统菜单栏，Windows/Linux 使用窗口内菜单 |
| `src-tauri/src/notify.rs` | **A1** 后台通知轮询 → 原生系统通知（接后端 `automations/notifications/list`） |
| `src-tauri/src/update.rs` | **A3** 一键自动更新：拉后端 manifest → 验签 → 安装 → 重启 |
| `src-tauri/tauri.conf.json` | 窗口/打包/deep-link scheme/资源/**updater 配置（pubkey + endpoints）** |
| `src-tauri/installer-hooks.nsh` | Windows 首装时选择“本机服务”或“仅客户端”；静默更新不重复询问 |
| `resources/server-bootstrap/install-local-server.ps1` | Windows 用户目录内创建 Python 环境并安装随包 CE 服务 |
| `resources/server-bootstrap/install-local-server.sh` | macOS 应用数据目录内准备独立 Python 运行时并安装随包 CE 服务 |
| `scripts/prepare-bundle.mjs` | 发行构建前生成同版本 CE 服务资源和 `desktop-bundle.json` |
| `scripts/ce-payload.mjs` | 在公开 CE 仓校验版本标识并只暂存 tracked tree，FULL 仓仍走生成器 |
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
- **本机服务一键安装**（菜单「文件 → 本机服务…」）：Windows 和 macOS 后端不可达时也会自动
  显示。安装过程提供
  阶段进度、实时日志、失败重试和健康检查；客户端更新携带新 CE 资源时自动升级服务代码，
  `data/` 目录保持不变。远程模式下点击安装会先在当前页面完成安装，服务通过健康检查后才切换
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
- Windows 本机服务首次安装需要联网下载 Python wheels。未安装 Python 3.11+ 时，引导脚本会
  优先用 `winget` 为当前用户静默安装；系统同时缺少 Python 和 `winget` 时，进度页会给出可重试错误。
  Node.js 20+ 缺失时也会尝试通过 `winget` 补齐；这一步失败不阻断核心服务，但 React 建站和高级
  PDF 渲染会保持降级状态。
- macOS 本机服务首次安装会在应用数据目录下载独立的 `uv` 和 Python 3.11，不修改系统 Python
  或 shell 配置。安装仍需联网下载 Python wheels；Node.js 20+ 仅影响可选的建站和高级文档能力，
  缺失时不阻断核心服务启动。
- 本机服务数据位于应用本地数据目录的 `local-server/data`。Windows 的默认位置是
  `%LOCALAPPDATA%\com.hugagent.desktop\local-server\data`，macOS 的默认位置是
  `~/Library/Application Support/com.hugagent.desktop/local-server/data`。卸载桌面客户端默认保留
  该目录，避免误删用户数据；确认不再需要后可手动删除。
- Linux 托盘依赖 libayatana-appindicator；Wayland 下全局快捷键（Ctrl+Shift+Space）兼容性因桌面
  环境而异。
- 依赖版本号（tauri 插件、axum/reqwest 等）以实际 `cargo build` 为准；个别 capability
  permission 标识符若构建报错，按报错提示微调 `capabilities/default.json`。
