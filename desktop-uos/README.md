# HugAgentOS UOS 1070 桌面客户端（Electron 43）

> [English](README_EN.md)

`desktop-uos/` 是面向 **UOS 统信 1070、aarch64、glibc 2.28** 的独立桌面发行线。
它固定使用 Electron `43.4.1`，只生成 Debian `arm64` 的 `.deb`，不依赖
WebKitGTK 4.1，也不把普通 AppImage 作为 UOS 兼容交付物。

现有 `desktop/` Tauri 2 客户端继续服务 Windows、macOS 和已验证的 Linux x86_64；
两条发行线复用同一套 React 前端、CE 服务归档、Python 依赖档案和后端登录/更新接口。

## 已迁移能力

- 本机、云端和双模式初始化；双模式保持“云端身份 + 本机执行面”。
- `hugagent://` deep-link 登录、一次性票据兑换、会话落盘与退出清理。
- 回环反代、session cookie 注入、SSE 流式透传、文件/站点/文档路由。
- 本地项目请求分流、身份桥、模型配置与能力令牌同步。
- 无窗口菜单栏的简洁外观（保留系统标题栏与窗口控制）、托盘、关闭到托盘偏好、
  `Ctrl+Shift+Space` 快速问答。
- 本地文件夹选择/授权、文件管理器打开、后台任务原生通知。
- 完整包内离线 CPython 3.11 安装、SHA-256 校验、安全解压、原子激活、数据备份和失败回滚。
- `.deb` 更新检查、SHA-256 验证、UOS 系统授权安装和重启。

渲染进程启用 `contextIsolation`、sandbox，关闭 Node 集成；只通过窄化 preload
桥暴露退出登录命令。来自页面的 cookie 和身份桥头会被反代剥离后由主进程重新注入。

## 构建目标

| 命令 | 构建机 | 产物 | 本机模式 |
|---|---|---|---|
| `npm run build` | 原生 Linux aarch64；UOS 1070 基线优先 | 完整 ARM64 `.deb` | 支持 |
| `npm run build:thin` | Linux x86_64 或 aarch64 | 云端精简 ARM64 `.deb` | 不支持 |
| `npm run pack:thin` | Linux x86_64 或 aarch64 | 未打包目录 | 不支持 |

完整包故意拒绝在 x86_64 上交叉生成。外壳可以交叉打包，但私有 Python 和所有二进制
wheel 必须在 aarch64 上真实执行自检；否则无法证明它们符合 UOS 1070 的 CPU 与 glibc
基线。ARM64 锁文件为 `desktop/requirements-desktop-linux-aarch64-py311.lock`，解析平台
固定 `aarch64-manylinux_2_28`。

## 构建步骤

构建机需要 Node.js `>=22.12`、Python `>=3.11`、uv `0.11.33`、`dpkg-deb`、`file`、
`readelf`。完整构建还需要网络下载锁定 wheel；最终用户安装和首次启动均不访问 PyPI。

```bash
cd desktop-uos
npm install
npm test

# UOS 1070 / Linux aarch64 原生构建机：完整离线包
npm run build

# x86_64 构建机：验证 Electron 43 ARM64 外壳
npm run build:thin

npm run verify:deb -- "dist/HugAgentOS UOS_0.5.17_uos1070_arm64.deb"
```

产物目录为 `desktop-uos/dist/`：

- `HugAgentOS UOS_<version>_uos1070_arm64.deb`
- `latest-uos.json`（包含 `platforms.linux-aarch64.url`、`sha256`、`format=deb`）

验证脚本会拆包确认 Debian 架构为 `arm64`、声明 `libc6 (>= 2.28)`、Electron 主程序
确为 AArch64，且最高 GLIBC 符号不超过 2.28。

## 发布更新

```bash
cd desktop-uos
npm run release:manifest -- \
  --deb "dist/HugAgentOS UOS_0.5.17_uos1070_arm64.deb" \
  --existing /path/to/current/latest.json \
  --output /path/to/desktop_release \
  --notes "UOS ARM64 更新说明"
```

脚本会把 `.deb` 和合并后的 `latest.json` 准备到 `--output`：同版本保留其它平台条目，
版本变化则建立新平台集合。将该目录同步到后端 `DESKTOP_RELEASE_DIR` 即可。
UOS 客户端拒绝缺少 SHA-256、非 `.deb` 或非 `linux-aarch64` 的更新条目。

## Electron 43 生命周期

Electron 43 是这条 UOS 1070 发行线的兼容基线，不等于无限期获得上游安全支持。官方计划
在 2027 年 1 月结束 43.x 支持。生产维护应每月升级到最新 43.x 补丁并回归 UOS 1070；
同时维护 Electron 44+ 的兼容验证分支，在满足 glibc 2.28 和 UOS 安装验收后切换主线。
