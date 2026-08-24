# UOS 统信 1070 aarch64 桌面端

> [English](../../en/deployment/uos1070-desktop.md)

UOS 1070 aarch64 使用仓库中的 `desktop-uos/` Electron 43 发行线。交付格式固定为
Debian ARM64 `.deb`；它不依赖系统 WebKitGTK 4.1，因此不受 Tauri 2 在 Debian 10 / UOS
1070 上的 WebView 依赖阻断。

## 支持范围

| 项目 | 生产基线 |
|---|---|
| 操作系统 | UOS 1070 桌面版，aarch64 |
| libc | glibc 2.28 |
| 桌面壳 | Electron 43.4.1 |
| 安装包 | Debian `arm64` `.deb` |
| 本机运行时 | 私有 CPython 3.11，`aarch64-manylinux_2_28` 锁 |
| 数据目录 | `~/.hugagent` |
| 配置目录 | `~/.config/com.hugagent.desktop` |

完整包支持本机、云端和双模式，能力与现有桌面端一致：deep-link 登录、回环反代、流式
对话、托盘/快捷问答、原生通知、本地文件夹项目、双上游路由、身份与模型配置桥接、离线
本机服务安装/升级/回滚、SHA-256 验证更新。

## 构建与验证

完整包只能在原生 Linux aarch64 构建机生成，推荐直接使用 UOS 1070 构建机。x86_64
机器只能交叉生成不含 Python 运行时的精简包。

```bash
cd desktop-uos
npm install
npm test
npm run build
npm run verify:deb -- "dist/HugAgentOS UOS_0.5.15_uos1070_arm64.deb"
```

验收时至少检查：

1. `dpkg-deb --field <包> Architecture` 返回 `arm64`；
2. Electron 主 ELF 的最高 GLIBC 引用不超过 2.28；
3. 断网冷启动完整包，本机服务安装阶段没有 `pip install`、uv 或 Python 下载；
4. 本机、云端和双模式分别完成登录、SSE 对话、附件上传下载、文件夹选择和通知；
5. 双模式下云端项目进云端，本地文件夹项目进本机；
6. 使用测试版更新清单完成 SHA-256 校验、系统授权安装和重启。

具体命令、产物和发布步骤见 [`desktop-uos/README.md`](../../../desktop-uos/README.md)。

> Electron 43 官方支持计划在 2027 年 1 月结束。它是 UOS 1070 的当前兼容基线，生产
> 维护仍需跟进最新 43.x 补丁，并持续验证 Electron 44+ 后续迁移。
