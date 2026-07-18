---
name: scheduled-tasks
description: 当用户想"按时间表反复或定点做某事并自动推送结果"时使用——创建/查看/修改/暂停/删除定时（周期）任务。触发词：定时、周期、每天、每周、每月、每隔、到点、提醒我、自动发、cron、schedule、日报/周报/月报、按时推送。指导如何用定时任务工具正确设置 cron、执行内容与投递目标。
---

# 定时任务管理

当用户表达"按时间表反复或定点执行某事，并把结果自动发给我/发到本会话/发到某群"的意图时，
**必须用本插件提供的工具**完成，不要自己编造"已创建"。

## 可用工具（来自「定时任务管理」插件）
- `create_scheduled_task(cron_expression, prompt, name, deliver_to)` — 创建
- `list_scheduled_tasks(status)` — 列出（用户问"我有哪些定时任务"时用）
- `get_scheduled_task(task_ref)` — 详情 + 最近运行记录
- `update_scheduled_task(task_ref, cron_expression?, prompt?, name?)` — 修改
- `pause_scheduled_task(task_ref)` / `resume_scheduled_task(task_ref)` — 暂停/恢复
- `delete_scheduled_task(task_ref)` — 删除/取消

## 铁律（务必遵守）
1. **先调用工具，再回话。** 在工具返回 `ok: true` 之前，**绝不**说"已创建/已修改/已删除/已设置"。
2. **创建/修改/删除类请求必须落到工具调用**，不要只用文字描述应付。
3. 删除/修改时若 `task_ref` 匹配到多个任务（返回 `need_clarification`），**先向用户确认**具体是哪一个，禁止猜删。

## cron 表达式（5 段：分 时 日 月 周，Asia/Shanghai 时区）
| 需求 | cron |
|---|---|
| 每天 9:00 | `0 9 * * *` |
| 每天 16:10 | `10 16 * * *` |
| 每周一 9:00 | `0 9 * * 1` |
| 每月 1 号 8:00 | `0 8 1 * *` |
| 每小时整点 | `0 * * * *` |
| 每 5 分钟 | `*/5 * * * *` |

把用户的口语时间（"每天早上九点半""每周五下午六点"）准确翻译成 cron。

## prompt 要自包含
到点执行时**没有当前对话上下文**，所以 `prompt` 要写清"做什么、产出什么"，
例如"汇总今天的销售数据并生成 Markdown 日报"，而不是"汇总一下"。

## 结果发到哪（deliver_to，一般留空）
- **留空（默认）**：自动——在飞书等渠道会话里创建 → 推回**该会话**；在网页里创建 → 到点生成一条
  **站内侧栏会话**（和在「自动化」页面手动建任务一样）。绝大多数情况都留空。
- **"inapp"**：用户明确说"发到我的网页/站内/不要发群里"时用 → 只发站内。
- **指定别的会话**（用户说"往运营群/给某某发"，而不是当前会话）：
  1. 先调 `list_channel_conversations` 拿到会话列表（含 title 和 conversation_id）；
  2. 按用户说的群名/对象匹配 title，取其 `conversation_id`；
  3. 作为 `create_scheduled_task` 的 `deliver_to` 传入。
  **不要凭空臆造 conversation_id**，必须来自 list_channel_conversations。

判断"当前会话是哪个"不需要你猜——系统已按用户发消息的来源自动确定；你只在用户要发到**别的**会话时才需要 list + 指定。

## 要让任务产出并投递文件（文档/表格/PDF/PPT）
投递是**自动**的，你不用、也没有任何"发送文件"的工具可调；但有一条硬前提：到点执行是一次
**全新、无上下文的 run**，**只有被 `pin_to_workspace` 固定到工作区的文件**才会 ①留在「我的空间」
②作为附件随投递目标（站内 / 飞书 / 钉钉 / 企微 / 微信会话，按 `deliver_to`）一起发出。**没 pin
的文件不投递、只发文字。** 这与渠道无关——飞书也好、站内也好，都是这一套。

所以当用户要"生成 XX 文档并发出去"时，`prompt` 必须写清"生成 XX 文档**并交付**"（不是只"汇总一下"）。
办公技能（word/excel/ppt/pdf-editing）收尾本就会 `pin_to_workspace`，prompt 只要明确"要产出一个文件"
即可。例：`prompt="汇总今天的工单生成 Markdown 日报文档并交付"`。

## 典型流程
1. 用户："每天早上 9 点把昨天的工单汇总发我。"
2. 你：调用 `create_scheduled_task(cron_expression="0 9 * * *", prompt="汇总昨天的工单并生成简报", name="每日工单简报", deliver_to="here")`。
3. 工具返回 `ok: true` 后，再用自然语言告诉用户任务名、执行时间与投递目标。
