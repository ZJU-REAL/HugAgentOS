# personal_agent.memory

Personal Agent 的记忆：按主题组织的 wiki。白天由干活的 agent 直接写主题页，每晚由 Dreaming 读会话记录补漏、核实、整理。设计文档：Personal Agent 记忆 Wiki 设计（混合写入版）。

## 文件

| 位置 | 内容 |
| --- | --- |
| `/workspace/USER.md` | 稳定偏好，只收用户原话，每轮注入 |
| `/workspace/MEMORY.md` | `## 置顶` 段只收用户原话，每轮注入；其余部分不注入 |
| `/workspace/memory/_schema.md` | 写作规则，平台写入，任何人不能改（`seed` 会改回去） |
| `/workspace/memory/_index.md` | 目录：`## Where to start` + 分组 + `## 待整理`；开头每轮注入 |
| `/workspace/memory/<type>-<name>.md` | 主题页，不注入，用到时读 |
| `/workspace/journal/YYYY-MM-DD.md` | 平台自动写的流水账（原 `memory/YYYY-MM-DD.md`），只追加，不注入，可搜索；Dreaming 用它的收据当待处理队列 |
| `/workspace/DREAMS.md` | 每次 Dreaming 的报告 |
| `<volume>/<agent>/memory-snapshots/` | 每次 Dreaming 前的 memory/ 快照，保留 14 份，沙箱里看不到 |

`memory/` 里不再有按日期的日记；旧日记由迁移移到 `journal/`。

## 模块

| 模块 | 职责 |
| --- | --- |
| `layout` | 路径、页面类型、文件名规则、大小上限、写入者常量 |
| `page` | 解析 frontmatter、事实行、来源标注、链接、`MOVED TO` 占位 |
| `sources` | 来源 id（`conv-` / `chan-` / `task-`）的生成、解析和存在性检查 |
| `lint` | 确定性检查：`check_page`（在线写入时）与 `check_wiki`（Dreaming 收尾时） |
| `store` | 记忆文件的唯一写入口：权限、租约、lint、加锁、原子写、记版本 |
| `models` / `versions` | 写入日志表 `personal_memory_versions`：记录、列出、清理（保留 30 天，每个文件最新一条永远保留） |
| `state` | `PersonalAgent.config` 里的 Dreaming 租约与创世回放状态 |
| `snapshots` | Dreaming 前的快照、恢复、列表 |
| `search` | `pa_memory_search` 的实现：wiki 页面 + `journal/` |
| `inject` | 每轮注入的内容：MEMORY.md 置顶段、索引开头；`trusted` 判断文件是不是 store 最后写下的样子 |
| `seed` / `templates/` | 预置 `_schema.md` 和空的 `_index.md`，并调用迁移 |
| `migration` | 旧布局一次性迁移；为每个注入文件记一条基线版本 |
| `dreaming/` | Dreaming 流水线：claim → recover → collect → snapshot → analyze → organize → finish / abort |

## 写入契约

- 记忆文件（`layout.is_memory(rel)` 为真）只能经 `store.write / edit / remove / revert` 写；其他工作区文件仍走 `workspace.files`。`journal/` 和 `DREAMS.md` 只由平台写。
- 写入者：`ONLINE` 主对话、渠道会话和后台任务；`USER_EDIT` 编辑器；`ANALYZER` / `ORGANIZER` Dreaming；`SYSTEM` 预置、迁移、撤销、快照恢复。谁能写哪个路径见 `store.allowed`。
- `ONLINE` 和 `ANALYZER` 写页面时必须带当前来源 id。`ONLINE` 新写或改写的事实行必须标当前来源；Dreaming 只能引用正在读的来源或 wiki 里已有的来源。页面原有的引用不再重新判断存在性。
- `ONLINE` 只有 `conv-` / `chan-` 来源（用户本人的对话）能写 USER.md 和 MEMORY.md；这两个文件不能有凭据。
- Dreaming 持有租约期间（`state.busy`），`ONLINE` 和 `USER_EDIT` 写入返回 409；在工作区锁里会从数据库重新读一次租约。
- 工作区锁在同一线程内可重入，最多等 5 秒；等不到时 store 返回 409。
- 每次写入在调用方事务里加一条版本，调用方负责提交。
- 沙箱的 Bash 能直接写 `/workspace`，绕过以上所有检查。所以 USER.md、MEMORY.md、`_index.md` 注入前要和最新版本的哈希比对，不一致就不注入，并提示用户在记忆面板里重新保存。把这几个路径在沙箱里挂成只读是更彻底的做法，还没做。
- 依赖方向：`memory` 依赖 `core`、`personal_agent.models`、`personal_agent.workspace.files`；`sources` 用 `conversation.conversations` 判断对话是否还在，`migration` 和 `dreaming` 用 `scheduling.schedules` 取用户时区，`dreaming.runs` 用 `access`。`workspace`、`tools`、`api` 依赖 `memory`，`memory` 不依赖它们（`migration` 只读 `workspace.constants` 里的模板路径）。
