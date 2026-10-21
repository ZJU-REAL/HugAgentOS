
# 宜搭登录态管理

## 严格禁止 (NEVER DO)

- 不要在代码中硬编码 Cookie 或凭证，Cookie 必须通过 `openyida login` 命令获取并缓存到 `.cache/cookies.json`
- 不要在 Cookie 失效时手动修改 `.cache/cookies.json`，必须重新执行登录流程

## 严格要求 (MUST DO)

- 执行任何宜搭操作前，必须先运行 `openyida env` 确认环境和登录态
- Cookie 失效时，重新登录后必须验证新 Cookie 可用（运行任意查询命令确认）
- **本技能不读写 memory**：登录态通过 `.cache/cookies.json` 持久化，不依赖跨会话的 memory 状态
- 用户提到登录"海外宜搭/国际版/日本宜搭/全球宜搭/Global YiDA"等海外语义时，必须给登录命令加 `--intl`（或 `--global` / `--overseas`），否则会生成中国钉钉的二维码，海外 DingTalk 无法扫码

## 适用场景

| 用户意图 | 触发条件 |
|---------|---------|
| 首次使用或 Cookie 失效 | 其他命令报 401/未登录错误时自动触发 |
| 切换账号/组织 | 先 `openyida logout` 再重新登录 |

## 触发条件

**正向触发**：
- 其他命令返回 401 / 未登录 / Cookie 失效错误时自动触发
- 用户明确说"登录"、"重新登录"、"扫码登录"
- 首次使用 openyida，尚无 `.cache/cookies.json`

**不适用场景（不要触发）**：
- 已有有效登录态（先用 `openyida env` 确认）
- 切换组织时（应先 `openyida logout` 再重新登录）

---


> 通常无需手动调用，其他命令在 Cookie 失效时会自动触发登录。

## 命令

```bash
openyida login
```

默认登录路径优先复用缓存；没有缓存时先判断当前机器是否具备本地浏览器登录能力（桌面环境 + Chrome/Edge/Chromium CDP，或可用 Playwright 兜底）。具备能力时默认打开浏览器扫码并自动写入 Cookie；只有云端/远程/无桌面/无浏览器能力时才使用二维码 handoff 或终端二维码。`openyida login --browser` 可显式强制本地浏览器登录。

若用户明确给出宜搭入口 URL，必须把该 URL 传给登录命令，或使用对应环境 flag；不要退化成裸 `openyida login`：

```bash
openyida login https://yida-group.alibaba-inc.com/
openyida login --alibaba
```

`https://yida-group.alibaba-inc.com/` 属于阿里内网宜搭，登录态应写入 `cookies-alibaba.json`；`www.aliwork.com`/`cookies-public.json` 表示走了默认公有云环境。

### AI 工具二维码登录模式

在 AI 对话框环境中没有有效缓存，且本地浏览器登录能力不可用时，`openyida login` 返回 `need_qr_scan` JSON，包含 `qr_image_markdown`、`agent_response_markdown`、`qr_image_file`、`qr_url`、`poll_command`、`browser_login_command` 和 `session_file`。

收到 `need_qr_scan` 后：

1. 必须在对话框中直接渲染 `qr_image_markdown`，或原样粘贴 `agent_response_markdown`；不要只展示 `qr_image_file` 文件路径或 `qr_url`
2. 让用户使用钉钉扫码并确认登录
3. 如果用户说明当前不是云端/远程电脑，或扫码失败，提示其改用 `browser_login_command`（通常是 `openyida login --browser`）
4. 用户确认扫码后执行 `poll_command`
5. 若返回 `need_corp_selection`，优先调用 OpenYida MCP 工具 `select_yida_login_organization`，传入 `session_file`，由 MCP 原生选择控件完成组织选择和 Cookie 写入

不要手动编造或写入 Cookie。多组织选择优先使用 `--corp-id <corpId>` 或 MCP 原生组织选择控件，不要把组织列表塞进普通聊天选择控件。

### 显式浏览器模式

需要强制本地浏览器登录时使用：

```bash
openyida login --browser
```

`--browser` 优先使用本地 Chrome / Edge / Chromium CDP，CDP 不可用时才用 Playwright 兜底。

下面这些是兼容旧版 AI 内置浏览器 handoff 的显式命令，只有用户明确要求内置浏览器 handoff 时才使用：

```bash
openyida login --codex
openyida login --qoder
openyida login --wukong
```

若宿主 in-app browser 缺少 Cookie 导出到 CLI 缓存的桥接能力，不要手动编造或写入 Cookie，改用默认登录或显式 `openyida login --agent-qr`。

### 显式二维码命令

需要强制使用 AI 工具二维码 handoff 时：

```bash
openyida login --agent-qr
```

该命令返回 `need_qr_scan` JSON，包含可直接渲染的 `qr_image_markdown` 和 `agent_response_markdown`。扫码后执行 `poll_command`。兼容旧命令 `openyida login --codex-qr`。

### 海外宜搭 / Global YiDA 登录

海外用户使用 DingTalk International（`login.dingtalk.io`）扫码，中国钉钉账号无法扫码海外二维码、反之亦然。海外登录必须显式声明环境，否则默认走国内 `login.dingtalk.com`。

**触发条件**（用户表达任意一项即视为海外场景，必须加 `--intl`）：

- 中文：海外、海外版、国际、国际版、全球、全球版、海外宜搭、国际宜搭、全球宜搭、日本、日本宜搭
- 英文：overseas、international、global、abroad、intl、Global YiDA

**推荐流程（已知能稳定调通业务 API）**：

```bash
openyida env switch intl     # 持久切到 intl，后续命令默认走海外
openyida login --browser     # 浏览器登录拿 cookies（写入 cookies-intl.json）
openyida app-list            # 验证 API 调通
```

**也支持的命令形式**（任选一种 flag 皆等价）：

```bash
openyida login --intl                  # 推荐写法
openyida login --global                # 别名
openyida login --overseas              # 别名
openyida login --agent-qr --intl       # AI 工具二维码 + 海外环境
openyida login --env intl              # 通用 --env 形式
```

`--intl` 标志会在生成二维码时使用 `login.dingtalk.io`，海外 DingTalk 可扫码完成 OAuth。

**Known limitation（海外环境）**：CLI 二维码登录（`--qr` / `--agent-qr`）拿到的 session cookies 当前不被 `www.yidaapps.com` 业务 API 完整认可——OAuth 链路本身能完成（可拿到 csrf_token / corp_id），但接下来调 `app-list` / `create-app` / `create-form` 等 API 时服务端返回 `errorCode:302 LOGIN FAILED`。在该问题修复前，海外环境优先用 `--browser` 模式登录；浏览器登录拿到的 cookies 可正常调用所有业务 API。

## 输出

```json
{"csrf_token":"b2a5d192-xxx","corp_id":"dingxxx","user_id":"1955225xxx","base_url":"https://abcd.aliwork.com"}
```

> `base_url` 取自登录后浏览器实际跳转到的域名，可能与 `config.json` 中的 `loginUrl` 不同。后续所有 API 请求使用此值。

## 错误处理

各命令通过响应体 `errorCode` 自动处理登录态异常：

| errorCode | 含义 | 处理方式 |
|-----------|------|---------|
| `TIANSHU_000030` | CSRF Token 过期 | 自动无头刷新 |
| `307` | Cookie 失效 | 自动重新登录 |

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| 扫码超时 | 重新执行 `openyida login`，二维码有时效限制 |
| 登录后 Cookie 仍无效 | 检查 `.cache/cookies.json` 是否正确写入，执行 `openyida env` 验证 |
| 反复登录失败 | 停止重试，提示用户联系开发同学 @天晟，不要自主尝试其他登录方案 |
| CSRF Token 过期（TIANSHU_000030） | 自动无头刷新，无需手动干预 |
| Cookie 失效（307） | 自动重新登录，无需手动干预 |
