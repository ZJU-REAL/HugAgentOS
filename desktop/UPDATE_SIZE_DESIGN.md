# 桌面端增量更新：运行时与更新包解耦设计（v3 · 最小方案）

> v1 初稿 → v2 吸收两轮对抗性审查 → v3 收敛范围：只保留「更新包不带运行时」必需的改动，
> 其余改进移到「推迟项」并写明理由。
>
> **实现状态**：第 1–5 节已实现（构建复用、小包构建 + CI、发布、客户端启动时获取、uv 版本校验 + 指纹输入测试），
> 还未在真实三平台构建上验证。本次改动修改了 `build-runtime.mjs`（指纹输入），所以下一个版本会产生新运行时；
> 建议这一版仍以完整包作为更新包发布，从再下一版（依赖不变）开始启用小包。
> 第 5 节中「写死 Python 补丁版本」改为「强制 uv 版本与锁定一致」：uv 版本已在指纹输入中，且决定了 `3.11` 解析到哪个补丁版本，
> 不需要猜测具体补丁号。

## 背景
完整版桌面包约 1GB，每次版本更新都要重新下载 1GB。

原因：三个平台 overlay（`src-tauri/tauri.{windows,macos,linux}.conf.json`）都把 `runtime-core.tar.gz`
（私有 CPython 3.11 + 依赖 + LibreOffice / git-bash / pandoc / OfficeCLI / Chromium，700MB+）打进 resources，
而 `tauri-plugin-updater` 是整包替换。

客户端已经按 `dependency_fingerprint` 复用运行时目录（`local_payload.rs`），依赖不变时不会重新解压，**但仍然会重新下载**。

## 目标
- 依赖不变的版本：更新下载量降到外壳 + server-ce（预计 50–150MB，以实测为准）。
- 依赖变化的版本：小包装好后，新版本启动时额外下载一次运行时。
- 首装 / 企业分发继续使用完整离线包；thin 与 UOS 不变。

## 设计

### 1 · 同一指纹只构建一次（发布侧，必需）
CI 不缓存 `desktop/generated`，而且运行时构建不可复现（macOS 签名带时间戳、`__pycache__` 等）。
如果每次发版都重建，同一指纹会对应不同字节，服务器上的文件就无法和安装包里的 `archive_sha256` 对上。

规则：
- `build-runtime.mjs` 先查发布目录里是否已有 `runtime-<target>-<fingerprint>.tar.gz`。
  - 有：下载它和对应的 `runtime-manifest.json`，放进 `generated/`，现有缓存判断（`build-runtime.mjs:35-43`）直接命中；
  - 没有：照常构建，发布时上传。
- 结果：完整包、小包、服务器上的运行时，同一指纹下字节完全一致。
- `runtime_id` 继续使用指纹，客户端目录结构和 `active.json` 都不变，不需要迁移。

### 2 · 多打一个不带运行时的小包（构建侧，必需）
- 在完整包构建之后，用 `--config` overlay 再执行一次 `tauri build`：
  - `beforeBuildCommand` 置空（`generated/` 已经准备好，不再跑 `prepare-bundle.mjs`）；
  - resources 里的 `runtime-core.tar.gz` 指向一个空的占位文件（thin 已经用过这种方式），
    **不改动** `generated/runtime-core.tar.gz`，避免破坏构建缓存；
  - `runtime-manifest.json` 照常打进去；
  - 编译期的 `HUGAGENT_DESKTOP_BUNDLE` 仍然是 `full`，不新增 flavor。
- 产物改名为 `*-update-*`，避免覆盖完整包，也避免被当成首装包分发。
- 只为 updater 需要的格式打小包：Windows NSIS、macOS `--bundles app`、Linux `--bundles appimage`。
- 代价：第二次 `tauri build` 可能触发主 crate 重新编译（config 会嵌入二进制），macOS 还要再签名一次。
  先实测耗时；如果太慢再考虑 `--no-bundle` + `tauri bundle` 的方式。
- Release CI（`desktop-release.yml`）目前直接调用 `tauri-action`，需要增加第二次构建，并上传运行时和小包。

### 3 · 发布（必需，小改）
- 运行时文件名：`runtime-<target>-<fingerprint>.tar.gz`，内容寻址，已存在且 sha 一致则跳过上传。
- 下载复用现有的 `GET /api/v1/desktop/download/{filename}`：它今天已经在提供 1GB 的完整安装包，支持 Range。
  **不新增端点，也不改 nginx。**
- `publish-desktop.py`：
  - 先发布运行时，再发布小包；
  - 发布小包时检查它所引用的运行时文件已经存在，否则拒绝发布。这样就不会出现 index 指向一个服务器上没有的运行时。
  - updater 条目指向小包；完整包作为 `installer` 字段一起登记，供官网下载页使用。
- 发布目录里的运行时文件不随版本清理（目前发布脚本本来就不删除任何文件）。

### 4 · 客户端：启动时获取运行时（必需）
这是唯一的下载路径。旧客户端不需要任何改动：它们照常下载并安装小包，**获取运行时的逻辑在新壳自己身上**。

`local_server/installer.rs` 的 `run_install`：
1. 如果资源里的运行时归档为空，并且 `runtimes/<fingerprint>` 不存在或校验失败：
   - 在 `stop_server()` 和数据备份**之前**下载（旧版本服务在此期间继续运行）；
   - 地址：`<base>/api/v1/desktop/download/runtime-<target>-<fingerprint>.tar.gz`，
     依次尝试当前 `update_base` 和编译期 `DESKTOP_UPDATE_BASE`；
   - 下载到 `runtimes/.stage-dl-<fingerprint>`（带 staging 标记，崩溃遗留的文件会被现有 prune 清掉），支持 Range 续传；
   - 用包内 `runtime-manifest.json` 的 `archive_sha256` 校验。这个清单随已签名的更新包到达，
     所以**不需要单独给运行时签名**。
   - 使用独立的 HTTP 客户端：走系统代理，只设读超时。现有客户端是 `.no_proxy()` + 10 秒连接超时（`bootstrap.rs`，取值见 `net.rs`），不适合下载大文件。
2. 下载完成后，后续流程与今天相同：解压 → `validate_runtime` → 提交。
3. 失败：保持旧版本服务运行（今天 `run_install` 失败时本来就会恢复旧版本），初始化页 / 本机服务页提示错误并提供重试，
   同时给出完整安装包的链接。

`local_payload.rs`（必需的小改）：
- `runtimes/<fingerprint>` 已存在且校验通过时，跳过 `verify_file_hash(runtime_archive)`（`:205`）。
- 同样情况下，`ensure_free_space`（`:206`）只计算 source 部分。
- `run_install` 开头的 `runtime_archive.is_file()` 检查保留；空文件不再被当作错误。
- `installer.rs:79` 的「不会下载 Python…」日志改掉。

覆盖的场景：
- 旧客户端直接升级到小包；
- 纯远程用户之后切换到本机模式，或者第一次点「开始初始化」；
- Dual / hybrid-only 用户（启动时同样会走 `run_install`）；
- 运行时目录被删除。

### 5 · 运行时变化检测（必需部分）
有了「同一指纹只构建一次」，最坏的情况是：运行时**应该**变了，但指纹没变，于是新版本继续复用旧运行时。
这不会造成内容不一致，但改动发不出去。本期只补上已知会静默改变运行时的两个缺口：
- **Python 补丁版本**：`uv python install 3.11` 改成写死 `3.11.<patch>`，补丁号放进一个纳入指纹的文件；
  `assertUv()` 校验 uv 版本与 `requirements-desktop.txt` 中的 `uv==` 一致。
- **指纹输入列表**：加一个测试，检查 `build-runtime.mjs` 读取的仓库文件都在 `DESKTOP_RUNTIME_INPUT_FILES` 里。

### 6 · 平台注意事项
- **Windows**：updater 以 `/UPDATE` 调用 NSIS，不会触发卸载钩子；运行时在 `%LOCALAPPDATA%` 下，不受影响。
  小包不要用于手动安装或软件分发，文件名和下载页都要区分。
- **macOS**：运行时随签名一起归档；reqwest 写入的文件不带 quarantine。需要实测下载后的 LibreOffice / OfficeCLI 能正常启动。
- **Linux**：只给 AppImage 打小包。UOS deb 与 linux-aarch64 首期保持完整包。

## 上线
不需要过渡版本：获取运行时的逻辑在新壳里，旧客户端只负责下载和安装小包。
- 首个小包版本：建议选一个**依赖不变**的版本，这样所有用户都不会触发运行时下载，先验证小包链路。
- 回退：发布时把 updater 条目改回完整包即可，客户端不需要任何改动。

## 验收
- 依赖不变的版本：更新下载量 ≤ 150MB，重启后本机服务直接启动。
- 依赖变化的版本：新版本启动后下载一次运行时，期间旧服务可用；断网后重试能续传；sha 不一致时不激活。
- 需要实测：旧客户端直接升级、纯远程切本机、Dual 用户、未初始化用户；三平台 + 两种 Mac 架构。

## 工作量
| 步骤 | 范围 | 估计 |
|---|---|---|
| 1 + 5 复用已发布运行时、Python 版本固定、输入列表测试 | `build-runtime.mjs`、`desktop-dependencies.mjs` | 1–2 天 |
| 2 小包构建 + CI | tauri overlay、`desktop-release.yml` | 2 天 |
| 3 发布脚本 | `publish-desktop.py` | 1 天 |
| 4 客户端启动时获取 + `local_payload` 小改 + 页面提示 | `installer.rs`、`local_payload.rs`、`bootstrap.rs`、初始化页 | 3 天 |
| 三平台联调 | | 2–3 天 |

合计约 9–11 个工作日。

## 推迟项（v2 中有、本期不做）
| 项目 | 不做的理由 | 什么时候再考虑 |
|---|---|---|
| 更新前后台预取（含 `latest.json` 的 `runtime` / `package_flavor` 字段、单独签名的描述文件、文件锁、`pending.json`） | 纯体验优化：只影响依赖变化的版本（约 1/5），而启动时获取已经能保证正确性。预取会引入第二条下载路径、并发锁和第二套信任链，复杂度最高 | 上线后如果用户抱怨更新后等待时间长 |
| `runtime_id` 改用 sha + 迁移 | 「同一指纹只构建一次」后，同一指纹就是同一份字节，没有必要改 | 如果以后放弃复用规则 |
| 新增 `/runtime/{target}/{sha}` 端点、nginx 单独 location、X-Accel-Redirect | 现有 `/download` 今天已经在提供 1GB 安装包，运行时下载不会比现状更重 | 观察到下载挤占 API 连接时 |
| 发布事务化、远程预检、保留与清理策略 | 「先运行时、后小包、缺运行时拒绝发布」已经足够；目前本来就不清理 | 发布目录空间告警时 |
| hash 锁 + `--require-hashes`、`--python-platform`、agentscope 构建依赖、Chromium sha、npm 版本、换行符、inventory、每周重建 | 这些是**既有的构建可复现性问题**，与本方案无关；有了复用规则，它们不会导致客户端拿到不一致的运行时 | 单独做一次构建可复现性治理 |
| `/download` 白名单 | 既有的安全问题，与本方案无关 | 单独修复（暴露的都是本就公开或无内容的文件，风险低） |
| 运行时分层、LibreOffice 按需下载、zstd、瘦身 | 进一步优化，不是本期目标 | 本期上线后 |
