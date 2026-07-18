# 环境准备与登录检测

> **本文档的功能**：执行任何宜搭操作前，按序检测并准备运行环境与登录态。**下方 6 步全部通过前，禁止创建应用/页面/表单或发布等真实资源操作。**

## 检查流程（按序执行，全绿才可操作）

先跑两条只读命令拿快照，再对照下表逐步判断：

```bash
openyida env --json                  # 环境快照：装没装 / AI 工具 / project / 登录态
openyida login --check-only --json   # 只读登录缓存，不触发登录、不建资源
```

| # | 检查项 | 判断依据 | 未通过时处理 |
|---|--------|---------|-------------|
| 1 | openyida 是否安装 | `env` 能跑并返回 `{"ok": true}` = 已装 | 报 `command not found` / 非零退出 → `npm install -g openyida`（先确认 Node ≥16） |
| 2 | openyida 版本 | `env` 能跑但报错 / 行为异常 = 版本过旧 | `npm install -g openyida@latest` 后重试；仍异常 → `openyida doctor` 逐项体检 |
| 3 | Node 版本 | 需 **Node.js ≥ 16**；`env` 只报告 `system.node`，不做校验 | 安装或运行报版本错 → 先升级 Node，再装/升级 openyida |
| 4 | 登录态 | `login.loggedIn` 为 true = 已登录 | 为 false → `openyida login`；指定入口须带 URL 或 flag（见「多环境登录」），勿退化成裸 `openyida login` |
| 5 | project 工作目录 | `active.projectRootExists` 为 true | 为 false → `openyida copy`（非悟空工具须先 `cd` 到工程根目录，见「初始化 project」） |
| 6 | 特殊环境 | 悟空 / Codex | 悟空命令连续失败 → 转人工诊断；Codex → 内置浏览器 handoff（见对应章节） |

## 命令速查

```bash
npm install -g openyida            # 安装
npm install -g openyida@latest     # 更新到最新（命令报错多因版本旧，更新后重试即可）
```

| 命令 | 定位 | 何时用 |
|------|------|--------|
| `env --json` | 机器可读快照：装没装 / 工具 / project / 登录 | 每次操作前（主命令）。恒返回 `ok:true`，`system.node` 仅报告不校验 |
| `login --check-only --json` | 只读登录态，不触发登录/不建资源 | 登录 / Codex handoff 后二次验证缓存写入 |
| `doctor` | 人读深度体检：校验 Node ≥16（ERROR）、npm ≥7（WARNING）、config.json、登录态、网络、浏览器登录；支持 `--fix` / `--report`，无 `--json` | `env` 报错或行为异常时排查 |

## env --json 关键字段

| 字段 | 含义 | 用途 |
|------|------|------|
| `ok` | 恒为 `true`（命令能跑即为真） | 不能用来判断是否安装 |
| `system.node` | 运行时 Node 版本 | 仅报告，是否达标看 `doctor` |
| `active.tool` | 当前活跃 AI 工具（悟空/OpenCode/Aone 等） | 决定 project 目录位置 |
| `active.projectRootExists` | `project/` 是否存在 | 为 `false` → `openyida copy` |
| `active.isWukong` | 是否悟空环境 | 决定 project 目录位置 |
| `login.loggedIn` / `login.canAutoUse` | 是否已登录 / 可自动使用 | 为 `false` → `openyida login` |
| `login.corpId` / `login.baseUrl` | 组织 ID / 域名 | corpId 一致性检查 |

不带 `--json` 直接跑 `openyida env` 时，输出报告分三块：

| 报告项 | 显示内容 |
|--------|---------|
| AI 工具检测 | 当前活跃的 AI 工具（悟空 / OpenCode / Aone Copilot 等） |
| 当前生效环境 | 项目根目录路径 |
| 登录态检测 | 是否已登录、域名、组织 ID；显示"未登录"就先 `openyida login` |

## 多环境登录

登录指定入口时命令必须带该 URL 或环境 flag，否则会落到默认公有云 `www.aliwork.com` / `cookies-public.json`。例如阿里内网 `https://yida-group.alibaba-inc.com/`：

```bash
openyida login https://yida-group.alibaba-inc.com/
openyida login --alibaba                              # 等价简写
```

## Codex 浏览器登录 handoff

Codex 默认返回内置浏览器 handoff：用 Browser Use 打开 `login_url`，由钉钉/宜搭页面承接扫码和组织选择；若 Browser Use 不能直接打开外部 URL，先开临时本地 redirect 页再跳转。登录完成后**必须**再执行 `openyida login --check-only --json` 验证缓存写入，验证通过前不得创建真实资源。

## 悟空（Wukong）降级规则

悟空环境本地命令入口连续失败时，不要继续重试，也不要判定为登录失败。转人工协同诊断：请用户在可用终端执行以下低风险命令并贴回输出：

```bash
openyida -v
openyida env --json
openyida login --check-only --json
```

确认 `loggedIn` / `can_auto_use`、`csrf_token_found`、`corp_id_found`、`base_url_found` 等关键项前，禁止创建任何真实宜搭资源。

## 初始化 project 工作目录

`active.projectRootExists` 为 false（切换 AI 工具、新工程首次使用）时执行 `openyida copy`。目录位置随工具而定：

| AI 工具 | project 目录位置 | 执行要求 |
|---------|-----------------|---------|
| **悟空（Wukong）** | `~/.real/workspace/project`（专属 workspace，与工程目录无关） | 直接执行 |
| **其他工具**（Aone Copilot / Cursor / Claude Code / OpenCode 等） | `<当前工程目录>/project` | 须先 `cd` 到工程根目录再执行 |

> ⚠️ 对于非悟空工具，必须先 `cd` 到工程根目录再执行 `openyida copy`，否则 `project/` 会铺错位置。
