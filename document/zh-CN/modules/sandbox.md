# 沙箱执行系统

> 最后更新：2026-09-24

沙箱是 HugAgentOS 中智能体执行代码的隔离环境：模型在对话里调用 `bash` 跑命令、运行[技能](agent-skills.md)脚本、生成可下载产物，全部发生在沙箱里而非后端主进程。系统通过统一的 **Provider 协议**抽象出三种可切换的执行后端——从单机轻量的 script_runner 到带持久会话、快照恢复的 OpenSandbox，再到远端 MicroVM 集群的 Cube——上层工具代码对此完全无感。

按[版本划分](../editions/overview.md)：**轻量沙箱（script_runner）+ 沙箱工具/offload 基础设施属社区版 CE**；**持久沙箱（opensandbox / cube，会话保持、环境复用、快照恢复）属商业版 EE**——社区版派生树剔除这两个 provider 文件，工厂自动回退轻量实现。

沙箱遵循一个统一边界：**`session_id`（对话中即 `chat_id`）是唯一隔离键**。不同会话使用不同沙箱；同一会话的主智能体、内置子智能体、用户自建子智能体和批量项共享同一个 `/workspace`。智能体的模型上下文、工具权限和执行线程仍可独立，但不会再在会话内部创建第二个文件沙箱。

## Provider 协议（core/sandbox/protocol.py）

所有 provider 实现同一个 `SandboxProvider` Protocol，内部完成入口封装底层进程协议；script-runner 通过 sidecar HTTP 接口执行进程操作：

| 方法 | 职责 |
|---|---|
| `run_to_completion(req: ProcessRequest)` | 内部业务入口：等待完成并返回最终 ProcessResult |
| `start_process(req: ProcessRequest, yield_time_ms=60000)` | 启动进程，返回新增输出、状态、进程 ID 或退出码 |
| `write_stdin(session_id, chars="", yield_time_ms=60000)` | 等待并读取新增输出，或中断进程 |
| `stage_files(user_id, files)` | 把输入文件暂存到用户 myspace 缓存，返回沙箱内可引用的绝对路径 |
| `put_file(session_id, path, content)` | 写字节进沙箱指定路径（自动建上级目录） |
| `get_file(session_id, path)` | 从沙箱读文件字节 |
| `get_file_to_path(session_id, path, destination, max_bytes)` | 把沙箱文件分块流式写入后端临时文件，并在传输中强制校验大小上限 |
| `current_sandbox_id(session_id)` | 纯查询当前绑定的底层沙箱身份（用于检测沙箱被重建） |
| `health()` | 健康探测 |
| `admin_*` 系列 | 安全管理台只读视图（能力声明 / 实例枚举 / 单实例详情 / 池统计），不支持的能力抛 `SandboxAdminNotSupported` 由 UI 置灰 |

内部业务通过 await provider.run_to_completion(request) 一次调用取得 ProcessResult（stdout、stderr、exit_code、execution_time_ms），不管理进程 ID，也不调用模型工具。三个 provider 共用底层进程管理和结果收集；等待在服务端内部完成，只启动一次，失败不自动重跑。模型工具层继续使用 bash / write_stdin。默认无命令运行截止时间，调用者仍可显式指定 timeout；单次等待、网络请求超时和云沙箱生命周期与命令运行时限不同。

`ProcessRequest` 的两个关键字段：

- **`session_id`**：所有 provider 都用它路由会话工作区；相同 ID 跨调用保留 `/workspace` 文件，不同 ID 互不可见。OpenSandbox / Cube 还会据此复用底层容器、MicroVM 或 kernel；
- **`user_id`**：触发 myspace 文件可见性（bind-mount 或 seed），见下文 Plan F。

## 三个 Provider 实现

| Provider | 文件 | 形态 | 适用场景 | 版本 |
|---|---|---|---|---|
| `script_runner` | `script_runner_provider.py` | 单 sidecar + 按 `session_id` 分目录的持久文件工作区；每次命令仍由 setrlimit 子进程执行 | 单机部署、轻量代码执行，默认值 | CE |
| `opensandbox` | `opensandbox_provider.py` + `_opensandbox_*.py` | 阿里 OpenSandbox（Docker 容器 + Jupyter 持久 kernel），per-chat 持久会话 + 预热池 + 快照 | 多轮迭代分析、技能重工作流 | **EE** |
| `cube` | `cube_provider.py` | 腾讯 CubeSandbox（E2B 兼容 MicroVM），**远端节点**——后端通过 `e2b_code_interpreter` SDK 跨网访问，无本地 sidecar | 后端宿主机资源紧张 / 需要强隔离（MicroVM 级）/ 沙箱算力独立扩容的部署 | **EE** |

切换由环境变量 `SANDBOX_PROVIDER` 控制（`core/sandbox/factory.py` 单例工厂）。CE 树中 `opensandbox` / `cube` 模块不存在时，工厂打告警并**自动回退 script_runner**，配置不变也能跑通。

Cube 的设计取舍（远端节点版的代价）：所有语言统一走"写脚本文件 + `commands.run`"，不依赖 Jupyter；**无 host bind-mount**（myspace 文件由工具层经 `put_file` 物化、技能文件运行时按需推送，`CUBE_SKILL_PREPUSH` 控制预推）；无快照体系；`session_id` 仍绑定持久 MicroVM（首次 create、后续 connect 复用）。

## Agent 侧工具

`core/llm/tools/sandbox_tool.py` 向智能体注册四个工具（`agent_factory` 在 Phase 3.5 调用各 `register_*`）：

| 工具 | 作用 |
|---|---|
| `bash(command, timeout=None, yield_time_ms=60000)` | 启动命令，默认等待 60 秒；未完成返回进程 `session_id`，命令继续运行。省略 timeout 不设命令执行期限，保留 `Bash` 别名 |
| `write_stdin(session_id, chars="", yield_time_ms=60000)` | 等待同一进程并读取新增输出；空输入等待，Ctrl+C（\u0003）请求中断 |
| `sandbox_put_artifact(artifact_id, dest_path)` | 把平台 artifact（用户上传文件、图表工具产物等）的字节拷入沙箱路径——沙箱不会自动看到上传文件 |
| `sandbox_get_artifact(src_path)` | 把沙箱内文件流式登记为可下载 artifact；默认单文件上限 100 MiB——bash 产物不会自动出现在附件区 |


命令执行期限与等待窗口分开：首次等待范围 250–30000 毫秒，后续等待上限 300000 毫秒；等待到期不会终止命令。显式 `timeout` 仍作为执行期限，但不再截成 120 秒。所有命令和内部 Python/JavaScript 脚本统一走进程接口；旧同步执行入口和默认/最大执行时限配置已删除。进程 ID 绑定启动时的用户与会话，不能跨会话读取或中断。当前为非 PTY 执行，stdin 关闭，除空输入和 Ctrl+C 外的输入会被拒绝；Windows 无控制台时中断采用终止进程树。

框架保留最多 64 个进程记录，仅回收已完成记录；新增输出有界，省略内容通过 `output_omitted_chars` 明示。本地及 script_runner 的完整输出存于当前会话工作区的 `.__process_*/stdout.log` 和 `stderr.log`，通过 `output_files` 返回真实路径；单流超过 64 MiB 时停止命令，已写日志仍可读。OpenSandbox 后台日志合并 stdout/stderr；Cube 总输出超过 64 MiB 时停止。运行期间定期调用沙箱保活：OpenSandbox 可续租，Cube 的兼容服务可能不支持延长服务端 TTL，返回 `lifetime_note` 说明这一边界。进程会话是运行时状态，不保证跨后端/runner 重启恢复；显式关闭会话或服务正常退出会清理进程。团队项目在命令终态返回后同步源码；必须等待完成，不能把 running 当成已经保存。

沙箱会话标识由 `resolve_sandbox_session(sandbox_session_id, chat_id)` 解析：非空显式 ID 优先，未传或传空值时统一回落到 `chat_id`。主智能体、计划执行、批量项和所有子智能体因此使用同一个会话沙箱；子智能体结束时不会销毁它。

**MySpace 实时登记**：写在 `/myspace` 下的文件**就是**用户「我的空间」里的文件，两侧任何时刻看到的都应是同一份状态。登记由**文件系统本身**驱动（`core/myspace/watcher.py`）：镜像目录 `myspace_cache/{uid}/` 里发生任何写入或删除，都被登记回 artifact 账本，**不区分是谁写的**。

早先的做法是让会写文件的工具各自登记（write / edit / 文件增删改 / `bash` 前后拍目录快照）。这套写法的前提是"改动只可能从这几个口子进来"，而这个前提不成立：`nohup` 起的后台进程在命令返回之后才写文件，子智能体和批量任务在另一条协程里写，技能里的 CLI 直接写，MCP 服务端也写。每多一个入口就要多补一处登记，漏掉的那一处的表现是——文件躺在用户网盘的磁盘上，界面上看不见也删不掉，而每个新建沙箱都会把这份目录挂进来，成了"上个会话的残留中间文件"。

| 时机 | 方向 | 做什么 |
|---|---|---|
| 目录里发生写入 / 删除 | 沙箱 → 我的空间 | 登记成 artifact，或把被删的文件从「我的空间」软删 |
| 每次 `bash` 执行前 | 我的空间 → 沙箱 | 界面上的上传/改动落进镜像目录（bind mount 下即刻可见）；界面上删掉的文件同步从镜像移除 |

登记按文件性质分三类，**不是一律推**：

- **新文件**（还没有登记记录）→ 直接登记，不打断用户。内容已经落在用户空间的磁盘上，此时再拦只会造出"界面看不见却删不掉、还被下一个会话挂到"的隐形文件。
- **改动 / 删除用户已有的文件** → 过 `MYSPACE_WRITE_CONFIRM` 确认门。文件在磁盘上早就改了/删了，所以确认能做的不是拦住，而是在用户否决后**从对象存储把那一版还原回来**。问不到人时（没有活跃会话、子智能体、批量、定时任务）直接生效，不拒绝——拒绝拦不住已经发生的改动，只会让两边不一致。确认同样认用户自选的权限档，选了「完全放开」就不再逐个文件弹框。
- **用户已删除的文件绝不复活**：镜像里的残留副本一律不登记，反过来由正向对账清掉；只有删除之后沙箱又写了同名文件才算新内容。

其它要点：

- **登记不挂会话、也不挂产物卡片**：「我的空间」是用户级的一份目录，每个会话的沙箱都挂着它，文件事件里只有路径、没有会话身份——谁写的，这里无从得知。所以登记只记到用户名下（`Artifact.chat_id` 为空），产物区的文件卡片则完全不由登记器挂：卡片只能由**写文件的那一轮自己**挂（模型调 `pin_to_workspace`，或那一轮里的 Write/交付兜底，都走 `core.llm.workspace` 的 ContextVar），那里"哪个会话、哪个文件"都是确定的。曾经拿"这个用户当下随便哪个在跑的会话"顶上，结果是另一个会话写的文件被记到了这个会话名下，还在它的产物区里多出一张卡片。
- **判定基准是 artifact 记录，不是镜像缓存**：开了 bind mount 后沙箱的 `/workspace/myspace/{uid}` 就是后端 `myspace_cache/{uid}` 本身，"沙箱文件与镜像缓存一致"恒真、不能再当作"已登记"。这条拓扑差异曾让回写恒为空转。
- **"磁盘比账本新"是唯一依据**：后端自己往镜像目录写文件（界面上传的物化、否决后的还原）时会把 mtime 对齐到登记时间，于是监听器不会把后端自己的写入当成沙箱的新改动转头再登记一次——不必另外维护一张"这是我自己写的"的表。
- **每个 worker 都监听，认领去重**：确认条活在跑这个会话的那个进程里，所以监听器必须每个 worker 都跑（代价是 inotify watch 按「worker 数 × 目录数」占内核配额 `fs.inotify.max_user_watches`，目录极多的部署要相应调大，超限会**静默丢事件**）；同一次改动由 `core.infra.ephemeral` 的认领保证只处理一次，且优先交给托管着该用户会话的进程（它才弹得出确认条）。
- **沙箱目录不在本机时**（`script_runner` / `cube`：会话工作区在容器内、或整个沙箱在远端）由 `core/myspace/sandbox_sync.py` 在命令结束后把沙箱现状搬进镜像目录，之后的判定与登记完全同路。

历史欠账（本次改造上线前积压的未登记文件 / 该删没删的残留）是**一次性数据迁移**，用 `scripts/reconcile_myspace_mirror.py` 手工跑一次即可（`--dry-run` 先看规模，残留副本的清理加 `--prune-stale`）。它不做成每次启动都执行的扫描：那既是白跑，也会掩盖实时这条路上真正的漏洞。

### 大文件产物交付

`sandbox_get_artifact` 默认支持最大 100 MiB（104,857,600 字节）的单个文件。
`SANDBOX_ARTIFACT_MAX_BYTES` 是沙盒文件大小的**唯一开关**，同时约束三条链路：
`sandbox_get_artifact` 显式取件、`bash` 执行后自动收集产物（单文件与单批总量）、
artifact 送入沙盒、`bash` 执行后 `/myspace` 写回同步；backend 与 script-runner sidecar
读取同一变量。三种 provider 都实现
`get_file_to_path`：script_runner 使用原始 HTTP 响应流，OpenSandbox 使用
`read_bytes_stream`，Cube 使用 E2B `format="stream"`。后端按流式分块写入
临时文件，再通过文件接口复制到本地 artifact 目录或上传到 OSS；传输过程中
不会把完整二进制转换成 Base64，也不会把整份文件常驻后端内存。

沙箱 `execute` 返回的内联产物仍保持单文件 10 MiB、单次合计 20 MiB 的保护
上限，因为这些文件会进入工具 JSON。大型产物必须显式调用
`sandbox_get_artifact`。文件在传输前或传输中超过配置上限时，工具返回
`sandbox_artifact_too_large` 结构化错误；PDF 应按页拆分，其他格式应拆包或
降低体积后逐个交付。

## OpenSandbox 会话生命周期（EE）

```
            ┌── 预热池 SandboxPool（_pool.py，双桶）──────────────┐
            │ jupyter 桶: min_idle=2  持久会话用（含 Jupyter，~10s）│
            │ light 桶:   min_idle=2  ephemeral 用（仅 execd，~3s）│
            └──────────────┬───────────────────────────────────┘
   首次 bash               │ acquire
chat_id ──▶ _get_or_create_session ──▶ _Session（sandbox + CodeInterpreter + 语言 ctx）
                │                         │  后续调用复用；fire-and-forget renew 续期
                │ idle > 600s（reaper）    │  renew 连续失败 → stale 标记 → 下次重建
                ▼                         ▼
        回 user idle pool（Q2 暖复用，~7s 重连）
                │ idle > 1500s（snapshot worker）
                ▼
        park：take_snapshot → Ready → 写 DB → kill 容器
                │ 用户回来
                ▼
        restore：Sandbox.create(snapshot_id=…) → 文件系统全量恢复（kernel 冷启，无感）
```

要点（`_opensandbox_session.py` / `_opensandbox_internals.py`）：

- **per-chat 重沙箱**：一个会话一个带 Jupyter 的容器，变量、pip 包、`/workspace` 文件跨 bash 调用持久；
- **TTL 与续期**：沙箱服务端 TTL 取 `SANDBOX_IDLE_TTL_S`（默认 3600s，与空闲回收同一个数）；每次会话活动触发限频（60s）的后台 renew，不阻塞请求路径；renew 失败区分 lifecycle 信号（立即标 stale）与瞬时网络错误（连续 3 次才升级）；
- **双层暖池**：进程启动即预热通用双桶池；Plan F 开启后 user-bound 流量改走 per-user 的 `_JupyterUserPool`（避免挂了别人 myspace volume 的沙盒被串用），idle reaper（`SANDBOX_IDLE_TTL_S`，默认 3600s）把空闲会话的沙盒**洗净 kernel、清空 `/workspace` 后回 user idle 池**复用而非销毁。清空只保留 `myspace` 与 `skills` 两个挂载点并重建 `scratch`——容器跨会话复用，上一轮的脚本和中间结果留在 `/workspace` 会被下一个会话当成自己的上文；擦不干净就放弃复用、直接销毁。

## 快照持久化（EE）

完整设计见 [sandbox-snapshot-design.md](../../sandbox-snapshot-design.md)。目标：会话空闲时不白白占着 Docker 资源，又能在用户回来时**带着文件系统**满血复活。

- **park（雪藏）**：后台 worker 每 60s 扫描，对 idle 超过 `SANDBOX_IDLE_TTL_S`（默认 3600s）的会话发起 snapshot（实测 accept→Ready 约 60s，docker commit），Ready 后 upsert `chat_sandbox_snapshots` 表（chat_id 为主键）、擦净 `/workspace` 把容器还回 idle 池；单轮最多并发 park 3 个保护 docker daemon。**这是回收会话沙箱的唯一路径**——旧版另有一条 600s 的「直接擦、不快照」回收比 park 先跑，结果文件没了也没备份，现已合并成这一条；
- **restore（恢复）**：`_create_session_for` **优先查 DB snapshot**（~15–20s restore，仍快于全新创建）；本会话没有快照时才走 Q2 user idle 池（~7s 暖路径），都没有才全新建。顺序不能反——idle 池里的容器是擦干净的，端给有快照的会话等于把它的文件悄悄丢掉。从 snapshot 启动时 volumes 必须**重新声明**——docker commit 不保存 mount 配置，否则 `/workspace/skills/` 等 bind-mount 会丢；
- **一次性消耗**：snapshot 被用作启动镜像后立即标 1 小时短保留（立刻 DELETE 会因镜像层被新容器引用而 409）；
- **GC**：每小时清理过期快照（DB 行 + 远端，默认保留 `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS=7` 天），删除冲突自动下轮重试；
- 总开关 `OPENSANDBOX_SNAPSHOT_ENABLED`（默认 true），关闭即回退"idle 即丢"的旧行为。

## MySpace bind-mount 直挂（Plan F，EE）

`OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED=true`（默认）时，创建 jupyter 沙箱直接把宿主机 `$HOST_STORAGE_PATH/myspace_cache/{uid}/` bind 进沙箱 `/workspace/myspace/{uid}/`（`_opensandbox_internals.py::_make_myspace_volume`）：

- 后端容器的 `/app/storage/myspace_cache/{uid}/` 与沙箱内路径指向**同一 host inode**——「我的空间」文件启动即可见，省掉旧路径整目录 HTTP PUT 的同步开销；
- 前提：`HOST_STORAGE_PATH` 与 docker-compose 挂 backend storage 用的宿主路径完全一致，且 OpenSandbox server 的 `allowed_host_paths` 包含该前缀；
- flag 关闭或 `user_id` 缺失时自动回退旧 HTTP PUT 同步路径（`_sync_inputs_to_sandbox`）；
- 配套隔离规则：挂了某用户 volume 的沙盒只能进**该用户**的 idle 池，绝不回通用池。

## 技能目录只读挂载

技能文件通过只读 bind mount 暴露在沙箱 `/workspace/skills/<id>`（`_make_skills_volumes`），挂的是**该用户自己的技能视图** `$HOST_STORAGE_PATH/sandbox_skills_u/<user_id>`：里面是他的私有技能加上指向公共技能的相对软链，公共技能库 `$HOST_STORAGE_PATH/sandbox_skills` 另挂到 `/workspace/skills_shared` 供软链解析（内置技能启动时同步进来、DB 技能按需物化，见[技能系统](agent-skills.md)）。于是所有技能仍是同一个路径，但别人安装的私有技能（含其 `secrets.json`）根本不在挂载范围内。预热的临时（light）沙箱会发给任意用户，只挂公共技能库。read-only 保证沙箱内不可篡改技能；目录 bind 是实时的，新导入技能立即可见。`HOST_STORAGE_PATH` 未配置时退回只挂内置源码树并告警。

## 超长工具结果落盘

超长工具结果由 `CompactingAgent` 保留有界节选，并通过 `SandboxOffloader`
将**完整文本**保存到当前工具工作目录的 `.offload/`。图片继续作为图片传给模型，
不会作为文本写入落盘文件。路径与本轮 `Read`、`bash` 使用的会话一致：

- 本机模式：`<本机工作区>/.sessions/<会话哈希>/.offload/`，不上传到云端。
- 云端 `script_runner`：`/workspace/.sessions/<会话哈希>/.offload/`；
  自定义工作区根目录时使用对应根目录。
- OpenSandbox / Cube（EE）：当前会话沙箱内的 `/workspace/.offload/`。

每次保存使用独立文件名，避免并行工具、重试或子智能体覆盖已有文件。
只有写入成功才向模型提供真实路径；模型应分页读取或定向搜索，避免把整份大文件
重新塞回上下文。写入被拒绝、服务不可达、没有持久会话或保存超时时，仍保留节选，
明确提示完整输出未保存，并建议缩小查询范围或分页重取，不将错误文本当成文件路径。
落盘等待上限为 30 秒，用户取消仍会传播。

上下文压缩及其 SDK 备用链路采用相同的失败处理：保存失败不撤销已经完成的压缩，
也不生成不存在的历史文件指引。历史归档为可读文本，图片及模型思考不属于此归档；
不能用它替代原始会话记录。落盘文件跟随当前工作区生命周期，不作为独立永久备份。

此修复需要更新云端后端及桌面客户端捆绑的本机后端；仅更新云端不能修复旧客户端。

## 管理员沙箱管理

- **只读监控（安全管理台）**：`api/routes/v1/config_security.py` 暴露 `/v1/config/security/sandbox/*`——总览、实例列表、单实例详情、快照列表、重建历史、生效配置；底层走 provider 的 `admin_*` 接口，按 `admin_capabilities()` 声明裁剪 UI（script_runner 无法枚举实例即置灰对应列）。
- **依赖重建（商业版 EE）**：`api/routes/v1/admin_sandbox.py`（`/v1/admin/sandbox/*`）聚合所有技能声明的 pip/apt 依赖（`core/services/skill_deps_aggregator.py`），管理员一键触发沙箱镜像重建：`script-runner` / `opensandbox` 目标走本地 `docker compose build`，`cube` 目标走 SSH 到远端节点重建模板并热切（`core/services/sandbox_rebuild_service.py` + `cube_template_builder`），可查看每次 run 的状态与日志。技能新依赖从此"烤入"镜像，无需手工改 Dockerfile。

## 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SANDBOX_PROVIDER` | `script_runner` | provider 选择：`script_runner` / `opensandbox` / `cube` |
| `SANDBOX_RUNNER_URL` | `http://hugagent-script-runner:8900` | script_runner sidecar 地址 |
| `SANDBOX_ARTIFACT_MAX_BYTES` | `104857600` | 沙盒文件大小唯一开关（默认 100 MiB）：取件、自动收集产物、送入沙盒、`/myspace` 写回同步共用 |
| `OPENSANDBOX_DOMAIN` / `OPENSANDBOX_API_KEY` / `OPENSANDBOX_IMAGE` | — | OpenSandbox 服务端与镜像 |
| `OPENSANDBOX_POOL_{JUPYTER,LIGHT}_{MIN,MAX}_IDLE` / `OPENSANDBOX_POOL_MAX_TOTAL` | 2/3、2/5、20 | 预热池水位 |
| `SANDBOX_IDLE_TTL_S` | 3600 | 沙箱唯一的时长参数：服务端 TTL = 空闲阈值 = 保活间隔基准；到点先 snapshot 再释放 |
| `OPENSANDBOX_SNAPSHOT_ENABLED` | true | 快照体系总开关 |
| `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS` | 7 | 快照保留天数 |
| `OPENSANDBOX_SNAPSHOT_WAIT_TIMEOUT_S` | 120 | 等 snapshot Ready 的轮询上限 |
| `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED` | true | Plan F myspace 直挂开关 |
| `HOST_STORAGE_PATH` | — | 宿主机 storage 真实路径（bind-mount 源） |
| `SANDBOX_SKILLS_DIR` | `$STORAGE_PATH/sandbox_skills` | 统一技能目录覆盖 |
| `MYSPACE_WRITE_CONFIRM` | true | /myspace 写操作用户确认硬保险 |
| `CUBE_API_URL` / `CUBE_API_KEY` / `CUBE_TEMPLATE` / `CUBE_API_SANDBOX_DOMAIN` | — | Cube 节点接入 |
| `CUBE_POOL_MIN_IDLE` / `CUBE_OWNER_TAG` | 2 / — | Cube 预热、多环境共用节点时的归属标签（回收时长同样取 `SANDBOX_IDLE_TTL_S`） |
| `CUBE_SKILL_PREPUSH*` | true / 20MB / 3 | 技能文件预推送开关/上限/并发 |
| `CUBE_NODE_SSH_*` / `CUBE_BUILD_*` | — | 管理员依赖重建的远端节点 SSH 与构建参数 |

完整清单见[环境变量参考](../deployment/environment-variables.md)。

## 相关源码

| 路径 | 说明 |
|---|---|
| `src/backend/core/sandbox/protocol.py` | Provider 协议与数据契约 |
| `src/backend/core/sandbox/factory.py` | provider 单例工厂 + CE 回退 |
| `src/backend/core/sandbox/script_runner_provider.py` | 轻量沙箱（CE） |
| `src/backend/core/sandbox/opensandbox_provider.py` | OpenSandbox provider 主体（EE） |
| `src/backend/core/sandbox/_opensandbox_session.py` | 会话/快照/park-restore worker（EE） |
| `src/backend/core/sandbox/_opensandbox_exec.py` | 执行路径 + idle reaper（EE） |
| `src/backend/core/sandbox/_opensandbox_internals.py` | volume 构造、metadata、user pool（EE） |
| `src/backend/core/sandbox/_pool.py` | 双桶预热池 |
| `src/backend/core/sandbox/cube_provider.py` | Cube 远端 MicroVM provider（EE） |
| `src/backend/core/llm/tools/sandbox_tool.py` | bash / write_stdin / sandbox_put_artifact / sandbox_get_artifact |
| `src/backend/core/llm/offloader.py` | 超长结果落盘 /workspace/.offload |
| `src/backend/api/routes/v1/admin_sandbox.py` | 依赖重建管理 API（EE） |
| `src/backend/api/routes/v1/config_security.py` | 安全管理台沙箱只读视图 |
| `src/backend/core/services/sandbox_rebuild_service.py` | 镜像/模板重建编排（EE） |
| `docker/Dockerfile.script-runner` / `docker/Dockerfile.opensandbox` / `docker/Dockerfile.cube-sandbox` | 三种沙箱镜像 |

相关文档：[技能系统](agent-skills.md) · [MCP 工具系统](mcp-tools.md) · [个人空间](projects-myspace.md) · [版本与许可](../editions/overview.md)
