# macOS 签名与升级

[English](../../en/deployment/macos-signing.md) · [桌面端本机模式](desktop-local-mode.md)

## 为什么升级后再次请求钥匙串授权

客户端把登录会话保存在 macOS 钥匙串。临时签名（ad-hoc）的应用身份绑定当前程序哈希，
重新构建后身份变化，旧的“始终允许”无法自动覆盖新版。
持续使用同一证书和应用标识可以提供稳定代码身份。固定自签证书不需要 Apple Developer 账号；
Developer ID Application 加公证则提供 Apple 分发信任。Tauri 更新包的 `.sig` 是独立的更新验签机制。

## 无开发者账号：固定自签证书

只在 Mac 构建机执行一次（在项目的 `desktop/` 下）：

```bash
python3 scripts/setup-macos-self-signing.py \
  --directory "$HOME/.config/desktop-signing" \
  --create --name Desktop-Local-Signing
```

脚本生成有效期 10 年的代码签名证书、加密私钥和 PKCS#12，导入独立构建钥匙串。
目录仅当前用户可读写，密码通过文件或标准输入传递，不出现在进程命令行和输出。
它保留用户原有钥匙串搜索列表和默认钥匙串，不修改系统信任。
对已经完成的目录再次执行会复用证书，遇到未完成目录会停止，避免意外覆盖已有身份。

后续每次构建都使用下面的入口。它在同一 SSH 会话中解锁签名钥匙串，
自动设置 `HUGAGENT_MACOS_SIGNING_MODE=self-signed` 和固定证书指纹：

```bash
# 在干净的发布 checkout 的 desktop/ 下，先准备文档要求的 Node/Rust/uv PATH。
HUGAGENT_RELEASE_BUILD=1 python3 "$HOME/.config/desktop-signing/signing.py" \
  --directory "$HOME/.config/desktop-signing" --run npm run build

python3 "$HOME/.config/desktop-signing/signing.py" \
  --directory "$HOME/.config/desktop-signing" \
  --run npm run verify:macos -- --bundle-dir src-tauri/target/release/bundle/macos
```

自签模式允许 CI 和发布构建，无需 Team ID 或 Apple 公证凭据。
验证器检查实际签名、应用标识、固定证书指纹及稳定身份要求，返回 `appleNotarized: false`。
这是固定签名包，仍未获 Apple 公证；首次下载的 Gatekeeper 提示不会因此消失。

保护并备份整个签名目录到受控的安全位置，勿放入 Git、普通产物同步目录或日志。
更换机器时继续使用原来的证书和私钥，勿重新生成。证书续期、丢失或切换到 Developer ID
都可能改变身份，需要单独安排迁移。此入口保留 Tauri updater 的配置；仍需原有更新签名密钥。

## 有开发者账号：Developer ID 与公证

1. 加入 [Apple Developer Program](https://developer.apple.com/programs/enroll/)，
   按 [Tauri 文档](https://v2.tauri.app/distribute/sign/macos/) 配置
   Developer ID Application 证书与配套私钥。
2. 设置 `HUGAGENT_MACOS_SIGNING_MODE=developer-id`（默认模式）、
   完整证书名称 `APPLE_SIGNING_IDENTITY` 及匹配的 `APPLE_TEAM_ID`。
3. 发布构建提供 `APPLE_ID`、`APPLE_PASSWORD`（应用专用密码），
   或 `APPLE_API_KEY`、`APPLE_API_ISSUER`、`APPLE_API_KEY_PATH`。
   凭据只存构建机安全配置或 CI Secrets。
4. 执行 `HUGAGENT_RELEASE_BUILD=1 npm run build`，再直接执行
   `npm run verify:macos -- --bundle-dir src-tauri/target/release/bundle/macos`。
   此模式还检查公证票据和 Gatekeeper 接受状态。

## 构建与 CI

构建前检查所选模式对应的证书和私钥；直接调用 Tauri 也会经过准备和打包前检查。
更换签名身份会改变 Mac 运行时指纹，防止复用旧身份签名的内置 Python 和工具。
有显式 Rust target 或 Cargo 自定义输出目录时，应传入实际的 `bundle/macos` 路径。
独立品牌包可指定 `--app /path/Example.app --identifier com.example.desktop`；
自签模式还可显式传入 `--mode self-signed --certificate-sha1 <40位指纹>`。

公开 CE Release workflow 的自签配置：

- Repository variable：`MACOS_SIGNING_MODE=self-signed`。
- Secrets：`APPLE_CERTIFICATE`（同一 PKCS#12 的 Base64）、`APPLE_CERTIFICATE_PASSWORD`、
  `APPLE_SIGNING_IDENTITY`（固定证书的 40 位 SHA-1 指纹）。
- 保留现有 Tauri updater secrets。无需配置 Apple 账号和 Team ID。

CI 先导入证书再签运行时和 App，最后验证实际产物。Developer ID 模式默认启用，另需
`APPLE_TEAM_ID`、`APPLE_ID` 和 `APPLE_PASSWORD`。草稿在全部检查通过后方可发布。
缺失所选身份时停止，不静默退回 ad-hoc，也不自动生成新证书。

仅本地测试可显式设置 `HUGAGENT_ALLOW_ADHOC=1 APPLE_SIGNING_IDENTITY=-`，同时不得设置
CI 或 `HUGAGENT_RELEASE_BUILD=1`。此包仍可能在升级时请求钥匙串授权，不通过产物验证。

## 旧用户迁移与验收

从临时签名切换到固定证书时，旧钥匙串条目可能再要求一次授权。确认应用来源后完成系统授权。
保留既有钥匙串服务名、账号键和应用标识；不要删除用户钥匙串、开放所有应用访问或改存明文。

实际客户端验收：旧版登录并授权 → 覆盖升级同一证书签名的新版 → 重新启动与登录。
还需检查重装、钥匙串锁定、用户拒绝和退出登录。系统锁定等原因仍可能要求授权。

依据：[Apple 代码签名与钥匙串身份](https://developer.apple.com/library/archive/technotes/tn2206/)、
[代码签名身份要求](https://developer.apple.com/documentation/technotes/tn3127-inside-code-signing-requirements)。
