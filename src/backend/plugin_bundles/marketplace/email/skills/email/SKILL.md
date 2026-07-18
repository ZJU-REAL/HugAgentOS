---
name: email
description: 收发与管理电子邮件（IMAP/SMTP）。当用户需要发送邮件、给某邮箱发文件/附件、回复或转发邮件、查看/搜索收件箱、阅读某封邮件、下载邮件附件、整理邮件（标记已读/加星/移动文件夹）、管理邮箱文件夹时使用。通过沙箱内的 himalaya CLI 以**用户本人邮箱身份**操作，支持 Gmail/Outlook/Exchange/网易企业邮/腾讯企业邮/自建邮箱等。
allowed_tools: bash, sandbox_get_artifact, sandbox_put_artifact
---

# 电子邮箱 Skill（himalaya）

通过沙箱内的 `himalaya` 命令以**当前用户的邮箱身份**收发和管理邮件。`himalaya` 二进制已随沙箱镜像预装，无需安装。用户的邮箱配置（IMAP/SMTP + 授权码）由后端注入到 `~/.config/himalaya/config.toml`，账号名固定为 `default`，**所有命令无需指定 `-a/--account`**。

## 连接前置（必须先确认）
本技能要求用户已在「插件库 → 电子邮箱 → 绑定邮箱」填写邮箱地址与授权码完成绑定（凭据已注入沙箱）。
- 执行任意业务命令前，先用 `himalaya folder list -o json` 确认能连上邮箱；若报「cannot find configuration」「no account」「authentication failed」之类，**停止并提示用户先到「插件库 → 电子邮箱」绑定邮箱账号**，不要尝试在聊天里收集邮箱密码。
- 绑定用的是各邮箱服务商的**授权码 / app password**（不是登录密码）：Gmail 用 App Password、QQ/网易/腾讯企业邮在邮箱设置里「开启 IMAP/SMTP 并获取授权码」。这些引导话术在绑定页已有，技能里只需提示用户去绑定。

> 凭据由后端注入到沙箱 `~/.config/himalaya/config.toml`，跨会话存活；技能本身不读写、不导出授权码明文。

## 全局约定
- 加 `-o json`（或 `--output json`）让输出结构化、便于解析；展示给用户时再转成自然语言。
- 邮件用数字 **id** 标识（来自 `envelope list`）。先 list 拿 id，再对该 id read/reply/flag。
- 默认文件夹是收件箱（INBOX）。其它文件夹用 `-f <folder>`（如 `-f Sent`）。

## 常用操作

### 查看收件箱 / 列邮件
```bash
himalaya envelope list -o json                 # 收件箱最近邮件（含 id/发件人/主题/日期/未读标记）
himalaya envelope list -f Sent -o json         # 指定文件夹
himalaya envelope list --page-size 20 -o json  # 控制条数
```

### 搜索邮件
```bash
# himalaya 的查询 DSL：from/subject/body/... 组合
himalaya envelope list 'from alice and subject 报告' -o json
himalaya envelope list 'since 2026-06-01' -o json
```

### 阅读某封邮件
```bash
himalaya message read <id> -o json             # 读正文
himalaya message read <id> --no-headers        # 仅正文纯文本
```

### 发送邮件（含附件）
用 MML 模板从 stdin 喂给 `template send`。附件用 `<#part ...>` 指向沙箱 `/workspace` 下的文件：
```bash
cat <<'EOF' | himalaya template send
To: someone@example.com
Cc: boss@example.com
Subject: 月度报告

您好，

附件是本月报告，请查收。

<#part type=application/pdf filename=/workspace/月度报告.pdf><#/part>
EOF
```
- 发纯文本就去掉 `<#part>` 行。多个附件就写多行 `<#part ...>`。
- 要发的文件先确保在沙箱 `/workspace` 里（用户上传的文件、或本会话生成的产物）。

### 回复 / 转发
```bash
# 回复（带原文引用）：编辑模板后 send
himalaya message reply <id> --output json      # 取回复模板
# 实操：用 template 工作流——先拿回复模板，填好正文再 send
himalaya message forward <id>                   # 转发模板
```
> reply/forward 的稳妥做法：`himalaya template reply <id>` / `himalaya template forward <id>` 取模板 → 在模板里补正文（必要时加 `<#part>` 附件）→ 管道给 `himalaya template send`。

### 下载附件
```bash
himalaya attachment download <id>               # 下到沙箱当前目录（/workspace）
```
下载后可用 `sandbox_get_artifact` 把文件交付给用户。

### 标记 / 整理
```bash
himalaya flag add <id> seen                      # 标记已读
himalaya flag remove <id> seen                   # 标记未读
himalaya flag add <id> flagged                   # 加星标
himalaya message move <id> <目标文件夹>           # 移动到文件夹
himalaya message delete <id>                      # 删除（进废纸篓/标记删除）
```

### 文件夹
```bash
himalaya folder list -o json                     # 列出所有文件夹
```

## 注意事项
- **Gmail 文件夹别名**：Gmail 的特殊文件夹名带 `[Gmail]/` 前缀（如 `[Gmail]/Sent Mail`、`[Gmail]/Trash`）。直接用 `-f Sent` 可能找不到——先 `folder list` 看真实名称，再用准确名称。
- **附件大小**：受邮件服务商限制（多数 20–50MB）；过大的文件改用网盘链接。
- **收件人确认**：发信前把收件人、主题、是否带附件向用户复述确认一次，避免误发。
- **失败处理**：命令报鉴权失败/连接超时，多半是授权码失效或服务器设置变更——提示用户到「插件库 → 电子邮箱」重新绑定，不要在聊天里索要密码。
