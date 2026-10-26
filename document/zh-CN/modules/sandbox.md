# 沙箱执行系统

> 最后更新：2026-10-18

沙箱是 HugAgentOS 中智能体执行代码的隔离环境：模型在对话里调用 `Bash` 跑命令、运行[技能](agent-skills.md)脚本、生成可下载产物，全部发生在沙箱里而非后端主进程。系统通过统一的 **Provider 协议**抽象出三种可切换的执行后端——从单机轻量的 script_runner 到带持久会话、快照恢复的 OpenSandbox，再到远端 MicroVM 集群的 Cube——上层工具代码对此完全无感。

OpenSandbox 基础执行、会话文件、个人空间与快照属于 CE 和 EE 共享能力。CE Docker 默认使用 OpenSandbox。团队挂载、组织治理和 Cube 属于 EE。无 Docker 安装与桌面本机模式继续使用本地执行。

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

内部业务通过 await provider.run_to_completion(request) 一次调用取得 ProcessResult（stdout、stderr、exit_code、execution_time_ms），不管理进程 ID，也不调用模型工具。三个 provider 共用底层进程管理和结果收集；等待在服务端内部完成，只启动一次，失败不自动重跑。模型工具层继续使用 Bash / write_stdin。默认无命令运行截止时间，调用者仍可显式指定 timeout；单次等待、网络请求超时和云沙箱生命周期与命令运行时限不同。

`ProcessRequest` 的两个关键字段：

- **`session_id`**：所有 provider 都用它路由会话工作区；相同 ID 跨调用保留 `/workspace` 文件，不同 ID 互不可见。OpenSandbox / Cube 还会据此复用底层容器、MicroVM 或 kernel；
- **`user_id`**：触发 myspace 文件可见性（bind-mount 或 seed），见下文 Plan F。

## 三个 Provider 实现

| Provider | 文件 | 形态 | 适用场景 | 版本 |
|---|---|---|---|---|
| `script_runner` | `script_runner_provider.py` | 单 sidecar + 按 `session_id` 分目录的持久文件工作区；每次命令仍由 setrlimit 子进程执行 | 单机部署、轻量代码执行，可选 | CE |
| `opensandbox` | `opensandbox_provider.py` + `_opensandbox_*.py` | Docker 容器、execd 命令执行、会话文件、预热池与快照；不保留 Python/JS 内存变量 | CE Docker 默认 | CE / EE |
| `cube` | `cube_provider.py` | 腾讯 CubeSandbox（E2B 兼容 MicroVM），**远端节点**——后端通过 `e2b_code_interpreter` SDK 跨网访问，无本地 sidecar | 后端宿主机资源紧张 / 需要强隔离（MicroVM 级）/ 沙箱算力独立扩容的部署 | **EE** |

通过 `SANDBOX_PROVIDER` 选择 provider，并让 `COMPOSE_PROFILES` 与之相符。缺失模块或 SDK 会直接报错，不会改用其他 provider。

Cube 的设计取舍（远端节点版的代价）：所有语言统一走"写脚本文件 + `commands.run`"，不依赖 Jupyter；**无 host bind-mount**（myspace 文件由工具层经 `put_file` 物化、技能文件运行时按需推送，`CUBE_SKILL_PREPUSH` 控制预推）；无快照体系；`session_id` 仍绑定持久 MicroVM（首次 create、后续 connect 复用）。

## Agent 侧工具

`core/llm/tools/sandbox_tool.py` 向智能体注册命令与产物工具（`factory/tools/native.py` 调用各 `register_*`）：

| 工具 | 作用 |
|---|---|
| `Bash(command, timeout=None, yield_time_ms=60000, run_in_background=False)` | 启动命令，默认等待 60 秒；未完成自动转后台。run_in_background=True 立即转后台。返回受管任务 ID，完成后自动通知原会话。省略 timeout 不设命令执行期限，命令工具统一使用首字母大写的 `Bash` |
| `write_stdin(session_id, chars="", yield_time_ms=0)` | 默认立即读取新增输出；最多短等 3000 毫秒。后台完成后自动通知会话。Ctrl+C（\u0003）请求中断 |
| `background_process(session_id)` | 接管已启动的原进程，不重新执行命令 |
| `list_processes()` | 列出当前用户在本会话的命令任务 |
| `sandbox_put_artifact(artifact_id, dest_path)` | 把平台 artifact（用户上传文件、图表工具产物等）的字节拷入沙箱路径——沙箱不会自动看到上传文件 |
| `sandbox_get_artifact(src_path)` | 把沙箱内文件流式登记为可下载 artifact；默认单文件上限 100 MiB——Bash 产物不会自动出现在附件区 |


命令执行期限与等待窗口分开：Bash 首次等待范围 250–60000 毫秒；普通会话 write_stdin 默认不等待，等待上限 3000 毫秒。provider 内部等待与评测工具保留各自契约；等待到期不会终止命令。显式 `timeout` 仍作为执行期限，但不再截成 120 秒。所有命令和内部 Python/JavaScript 脚本统一走进程接口；旧同步执行入口和默认/最大执行时限配置已删除。进程 ID 绑定启动时的用户与会话，不能跨会话读取或中断。当前为非 PTY 执行，stdin 关闭，除空输入和 Ctrl+C 外的输入会被拒绝；Windows 无控制台时中断采用终止进程树。

框架保留最多 64 个进程记录，仅回收已完成记录；新增输出有界，省略内容通过 `output_omitted_chars` 明示。本地及 script_runner 的完整输出存于当前会话工作区的 `.__process_*/stdout.log` 和 `stderr.log`，通过 `output_files` 返回真实路径；单流超过 64 MiB 时停止命令，已写日志仍可读。OpenSandbox 后台日志合并 stdout/stderr；Cube 总输出超过 64 MiB 时停止。运行期间定期调用沙箱保活：OpenSandbox 可续租，Cube 的兼容服务可能不支持延长服务端 TTL，返回 `lifetime_note` 说明这一边界。普通会话的命令账本保存任务状态与有界输出。OpenSandbox 使用原执行标识和日志游标恢复；不重跑命令。其他 provider 正常退出时停止进程；异常重启后标记 unknown。显式删除会话会停止任务。团队项目在命令终态返回后同步源码；必须等待完成，不能把 running 当成已经保存。

后台执行由服务端采集状态、保存日志并保持沙箱活动。模型可以执行独立工作；没有其他工作时，模型结束当前运行。完成事件在下一次推理前进入活动会话，或使用现有会话运行入口唤醒空闲模型。write_stdin 用于按需读取新增输出、短时等待和取消，不用于反复轮询。认证日志 API 的读取不消耗模型输出游标。聊天界面不展示独立的后台命令栏。页面预先订阅 /v1/commands/events 长连接，后端在唤醒运行就绪后立即推送运行标识，页面随即接入现有输出流，不等待任务轮询。重连时补发已就绪的运行；已结束的短回复从历史加载。

沙箱会话标识由 `resolve_sandbox_session(sandbox_session_id, chat_id)` 解析：非空显式 ID 优先，未传或传空值时统一回落到 `chat_id`。主智能体、计划执行、批量项和所有子智能体因此使用同一个会话沙箱；子智能体结束时不会销毁它。

**MySpace 实时登记**：写在 `/myspace` 下的文件**就是**用户「我的空间」里的文件，两侧任何时刻看到的都应是同一份状态。登记由**文件系统本身**驱动（`core/files/spaces/`）：镜像目录 `myspace_cache/{uid}/` 里发生任何写入或删除，都被登记回 artifact 账本，**不区分是谁写的**。

早先的做法是让会写文件的工具各自登记（write / edit / 文件增删改 / `Bash` 前后拍目录快照）。这套写法的前提是"改动只可能从这几个口子进来"，而这个前提不成立：`nohup` 起的后台进程在命令返回之后才写文件，子智能体和批量任务在另一条协程里写，技能里的 CLI 直接写，MCP 服务端也写。每多一个入口就要多补一处登记，漏掉的那一处的表现是——文件躺在用户网盘的磁盘上，界面上看不见也删不掉，而每个新建沙箱都会把这份目录挂进来，成了"上个会话的残留中间文件"。

| 时机 | 方向 | 做什么 |
|---|---|---|
| 目录里发生写入 / 删除 | 沙箱 → 我的空间 | 登记成 artifact，或把被删的文件从「我的空间」软删 |
| 每次 `Bash` 执行前 | 我的空间 → 沙箱 | 界面上的上传/改动落进镜像目录（bind mount 下即刻可见）；界面上删掉的文件同步从镜像移除 |

登记按文件性质分三类，**不是一律推**：

- **新文件**（还没有登记记录）→ 直接登记，不打断用户。内容已经落在用户空间的磁盘上，此时再拦只会造出"界面看不见却删不掉、还被下一个会话挂到"的隐形文件。
- **改动 / 删除用户已有的文件** → 过 `MYSPACE_WRITE_CONFIRM` 确认门。文件在磁盘上早就改了/删了，所以确认能做的不是拦住，而是在用户否决后**从对象存储把那一版还原回来**。问不到人时（没有活跃会话、子智能体、批量、定时任务）直接生效，不拒绝——拒绝拦不住已经发生的改动，只会让两边不一致。确认同样认用户自选的权限档，选了「完全放开」就不再逐个文件弹框。
- **用户已删除的文件绝不复活**：镜像里的残留副本一律不登记，反过来由正向对账清掉；只有删除之后沙箱又写了同名文件才算新内容。

其它要点：

- **登记不挂会话、也不挂产物卡片**：「我的空间」是用户级的一份目录，每个会话的沙箱都挂着它，文件事件里只有路径、没有会话身份——谁写的，这里无从得知。所以登记只记到用户名下（`Artifact.chat_id` 为空），产物区的文件卡片则完全不由登记器挂：卡片只能由**写文件的那一轮自己**挂（模型调 `pin_to_workspace`，或那一轮里的 Write/交付兜底，都走 `core.llm.runtime.workspace` 的 ContextVar），那里"哪个会话、哪个文件"都是确定的。曾经拿"这个用户当下随便哪个在跑的会话"顶上，结果是另一个会话写的文件被记到了这个会话名下，还在它的产物区里多出一张卡片。
- **判定基准是 artifact 记录，不是镜像缓存**：开了 bind mount 后沙箱的 `/workspace/myspace/{uid}` 就是后端 `myspace_cache/{uid}` 本身，"沙箱文件与镜像缓存一致"恒真、不能再当作"已登记"。这条拓扑差异曾让回写恒为空转。
- **"磁盘比账本新"是唯一依据**：后端自己往镜像目录写文件（界面上传的物化、否决后的还原）时会把 mtime 对齐到登记时间，于是监听器不会把后端自己的写入当成沙箱的新改动转头再登记一次——不必另外维护一张"这是我自己写的"的表。
- **每个 worker 都监听，认领去重**：确认条活在跑这个会话的那个进程里，所以监听器必须每个 worker 都跑（代价是 inotify watch 按「worker 数 × 目录数」占内核配额 `fs.inotify.max_user_watches`，目录极多的部署要相应调大，超限会**静默丢事件**）；同一次改动由 `core.infra.ephemeral` 的认领保证只处理一次，且优先交给托管着该用户会话的进程（它才弹得出确认条）。
- **沙箱目录不在本机时**（`script_runner` / `cube`：会话工作区在容器内、或整个沙箱在远端）由 `core/files/spaces/sandbox.py` 在命令结束后把沙箱现状搬进镜像目录，之后的判定与登记完全同路。

### 大文件产物交付

`sandbox_get_artifact` 默认支持最大 100 MiB（104,857,600 字节）的单个文件。
`SANDBOX_ARTIFACT_MAX_BYTES` 是沙盒文件大小的**唯一开关**，同时约束三条链路：
`sandbox_get_artifact` 显式取件、`Bash` 执行后自动收集产物（单文件与单批总量）、
artifact 送入沙盒、`Bash` 执行后 `/myspace` 写回同步；backend 与 script-runner sidecar
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

## OpenSandbox 会话生命周期（CE / EE）

```text
会话请求 → 校验身份与会话绑定 → 复用容器 / 从快照恢复 / 创建容器
        → execd 执行独立进程 → 保存会话文件 → 按活动续期
空闲达到 SANDBOX_IDLE_TTL_S → 创建快照 → 等待 Ready → 登记数据库
        → 清理工作区 → 归还对应用户的空闲池
用户返回 → 先恢复会话快照 → 重新声明挂载 → 执行下一条命令
```

- 一个会话复用同一容器中的文件。Python/JS 每次启动独立进程，不保留内存变量。
- 用户绑定的容器仅能交给同一用户，复用前清理上一会话的工作区。
- 基础实现保留个人空间和技能挂载。EE 扩展额外保留团队挂载并复核权限。
- CE Docker 默认不预热空闲容器。历史配置名中的 jupyter 表示用户会话池，不会启动 Jupyter。
- SANDBOX_IDLE_TTL_S 控制空闲快照阈值，默认 3600 秒。服务端硬到期时间另加回收排队、续期限频、快照请求和等待的宽限。每轮扫描先为排队会话续期，再公平分批创建快照。
- 带密钥的续期与快照请求统一使用 OPEN-SANDBOX-API-KEY 认证头。

## 快照持久化（CE / EE）

实现位于 core/sandbox/providers/opensandbox/snapshots.py。目标：会话空闲时不白白占着 Docker 资源，又能在用户回来时**带着文件系统**满血复活。

- **park（雪藏）**：后台 worker 每 60s 扫描，对 idle 超过 `SANDBOX_IDLE_TTL_S`（默认 3600s）的会话发起 snapshot（实测 accept→Ready 约 60s，docker commit），Ready 后 upsert `chat_sandbox_snapshots` 表（chat_id 为主键）、擦净 `/workspace` 把容器还回 idle 池；单轮最多并发 park 3 个保护 docker daemon。**这是回收会话沙箱的唯一路径**——旧版另有一条 600s 的「直接擦、不快照」回收比 park 先跑，结果文件没了也没备份，现已合并成这一条；
- **restore（恢复）**：`_create_session_for` **优先查 DB snapshot**（~15–20s restore，仍快于全新创建）；本会话没有快照时才走 Q2 user idle 池（~7s 暖路径），都没有才全新建。顺序不能反——idle 池里的容器是擦干净的，端给有快照的会话等于把它的文件悄悄丢掉。从 snapshot 启动时 volumes 必须**重新声明**——docker commit 不保存 mount 配置，否则 `/workspace/skills/` 等 bind-mount 会丢；
- **一次性消耗**：snapshot 被用作启动镜像后立即标 1 小时短保留（立刻 DELETE 会因镜像层被新容器引用而 409）；
- **GC**：每小时清理过期快照（DB 行 + 远端，默认保留 `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS=7` 天），删除冲突自动下轮重试；
- 总开关 `OPENSANDBOX_SNAPSHOT_ENABLED`（默认 true），关闭即回退"idle 即丢"的旧行为。

## MySpace bind-mount 直挂（CE / EE）

`OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED=true`（默认）时，创建用户会话沙箱直接把宿主机 `$HOST_STORAGE_PATH/myspace_cache/{uid}/` bind 进沙箱 `/workspace/myspace/{uid}/`（`_opensandbox_internals.py::_make_myspace_volume`）：

- 后端容器的 `/app/storage/myspace_cache/{uid}/` 与沙箱内路径指向**同一 host inode**——「我的空间」文件启动即可见，省掉旧路径整目录 HTTP PUT 的同步开销；
- 前提：`HOST_STORAGE_PATH` 与 docker-compose 挂 backend storage 用的宿主路径完全一致，且 OpenSandbox server 的 `allowed_host_paths` 包含该前缀；
- flag 关闭或 `user_id` 缺失时自动回退旧 HTTP PUT 同步路径（`_sync_inputs_to_sandbox`）；
- 配套隔离规则：挂了某用户 volume 的沙盒只能进**该用户**的 idle 池，绝不回通用池。

## 技能目录只读挂载

技能文件通过只读 bind mount 暴露在沙箱 `/workspace/skills/<id>`（`_make_skills_volumes`），挂的是**该用户自己的技能视图** `$HOST_STORAGE_PATH/sandbox_skills_u/<user_id>`：里面是他的私有技能加上指向公共技能的相对软链，公共技能库 `$HOST_STORAGE_PATH/sandbox_skills` 另挂到 `/workspace/skills_shared` 供软链解析（内置技能启动时同步进来、DB 技能按需物化，见[技能系统](agent-skills.md)）。于是所有技能仍是同一个路径，但别人安装的私有技能（含其 `secrets.json`）根本不在挂载范围内。预热的临时（light）沙箱会发给任意用户，只挂公共技能库。read-only 保证沙箱内不可篡改技能；目录 bind 是实时的，新导入技能立即可见。`HOST_STORAGE_PATH` 未配置时退回只挂内置源码树并告警。

## 超长工具结果落盘

超长工具结果由 `CompactingAgent` 保留有界节选，并通过 `SandboxOffloader`
将**完整文本**保存到当前工具工作目录的 `.offload/`。图片继续作为图片传给模型，
不会作为文本写入落盘文件。路径与本轮 `Read`、`Bash` 使用的会话一致：

- 本机模式：`<本机工作区>/.sessions/<会话哈希>/.offload/`，不上传到云端。
- 云端 `script_runner`：`/workspace/.sessions/<会话哈希>/.offload/`；
  自定义工作区根目录时使用对应根目录。
- OpenSandbox（CE / EE）与 Cube（EE）：当前会话沙箱内的 `/workspace/.offload/`。

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

- **只读监控（安全管理台）**：`features/security_console/api/config_security.py` 暴露 `/v1/config/security/sandbox/*`——总览、实例列表、单实例详情、快照列表、重建历史、生效配置；底层走 provider 的 `admin_*` 接口，按 `admin_capabilities()` 声明裁剪 UI（script_runner 无法枚举实例即置灰对应列）。
- **依赖重建（商业版 EE）**：`features/sandbox_ops/api/admin_sandbox.py`（`/v1/admin/sandbox/*`）聚合所有技能声明的 pip/apt 依赖（`core/capabilities/skills/deps/aggregator.py`），管理员一键触发沙箱镜像重建：`script-runner` / `opensandbox` 目标走本地 `docker compose build`，`cube` 目标走 SSH 到远端节点重建模板并热切（`features/sandbox_ops/rebuild.py` + `cube_template_builder`），可查看每次 run 的状态与日志。技能新依赖从此"烤入"镜像，无需手工改 Dockerfile。

## 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SANDBOX_PROVIDER` | CE Docker: `opensandbox` | provider 选择：`script_runner` / `opensandbox` / `cube` |
| `SANDBOX_RUNNER_URL` | `http://hugagent-script-runner:8900` | script_runner sidecar 地址 |
| `SANDBOX_ARTIFACT_MAX_BYTES` | `104857600` | 沙盒文件大小唯一开关（默认 100 MiB）：取件、自动收集产物、送入沙盒、`/myspace` 写回同步共用 |
| `OPENSANDBOX_DOMAIN` / `OPENSANDBOX_API_KEY` / `OPENSANDBOX_IMAGE` | — | OpenSandbox 服务端与镜像 |
| `OPENSANDBOX_POOL_{JUPYTER,LIGHT}_{MIN,MAX}_IDLE` / `OPENSANDBOX_POOL_MAX_TOTAL` | 2/3、2/5、20 | 预热池水位 |
| `SANDBOX_IDLE_TTL_S` | 3600 | 空闲快照阈值和保活间隔基准；服务端 TTL 另加快照与排队宽限 |
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
| `src/backend/core/sandbox/factory.py` | provider 显式选择；初始化错误直接上报 |
| `src/backend/core/sandbox/providers/script_runner/provider.py` | 轻量沙箱（CE） |
| `src/backend/core/sandbox/providers/opensandbox/provider.py` | OpenSandbox 共享 provider 主体 |
| `src/backend/core/sandbox/providers/opensandbox/` | 会话/快照/park-restore worker（EE） |
| `src/backend/core/sandbox/providers/opensandbox/execution.py` | 执行路径 + idle reaper（EE） |
| `src/backend/core/sandbox/providers/opensandbox/` | volume 构造、metadata、user pool（EE） |
| `src/backend/core/sandbox/providers/opensandbox/pool.py` | 双桶预热池 |
| `src/backend/core/sandbox/providers/cube/provider.py` | Cube 远端 MicroVM provider（EE） |
| `src/backend/core/llm/tools/sandbox_tool.py` | Bash / write_stdin / sandbox_put_artifact / sandbox_get_artifact |
| `src/backend/core/llm/context/offloader.py` | 超长结果落盘 /workspace/.offload |
| `src/backend/features/sandbox_ops/api/admin_sandbox.py` | 依赖重建管理 API（EE） |
| `src/backend/features/security_console/api/config_security.py` | 安全管理台沙箱只读视图 |
| `src/backend/features/sandbox_ops/rebuild.py` | 镜像/模板重建编排（EE） |
| `docker/sandbox/Dockerfile.script-runner` / `docker/sandbox/Dockerfile.opensandbox` / `docker/sandbox/Dockerfile.cube-sandbox` | 三种沙箱镜像 |

相关文档：[技能系统](agent-skills.md) · [MCP 工具系统](mcp-tools.md) · [个人空间](projects-myspace.md) · [版本与许可](../editions/overview.md)

Docker OpenSandbox 每个实例独占一个 bridge；仅受信任的后端与控制器接入，不发布实例宿主端口。用户隔离验收同时检查个人挂载、会话权限及跨沙盒 execd 不可达。

### 对象挂载恢复的用户归属校验

后端创建沙盒与控制器恢复挂载共用 `core/sandbox/sessions/metadata.py` 的用户标签定义。
控制器镜像在构建时复制该模块。移植产品时保留该产品现有的标签值，并重新构建控制器；
不要单独改动一端，也不要通过接受其它产品标签绕过归属校验。
恢复时继续校验当前用户、权限版本、挂载范围、凭据和只读属性。
校验冲突返回 409，并保留沙盒和工作区；挂载服务故障返回 503。
控制器升级后，先验证现有沙盒恢复和跨用户拒绝，再执行用户命令。

### 回收计数与多进程管理视图

控制器明确返回 DOCKER::SANDBOX_NOT_FOUND 时，后端清除失效会话并归还一次占用。
快照或续期超时、鉴权失败、普通路由 404 和服务错误都不能作为删除活容器的依据。
成功快照后才允许擦除并复用工作区。显式关闭与空闲回收使用相同的会话锁和共享认领。

OpenSandbox 的实例列表与实例计数读取控制器当前运行清单。
控制器查询失败时，计数不可用；不能把查询失败报告成零实例。
连接池诊断保留 scope=worker 与 worker_pid，表示当前后端进程的本地池，
不代表全部进程的用户配额。部署级名额由下文的 backend 名额账本统一执行。

## 共享容量与维护所有权

所有 OpenSandbox 创建都经过 `providers/opensandbox/admission.py`，守卫测试禁止其他位置直接调用
`Sandbox.create`。准入先在共享 ephemeral 存储（配置了 Redis 时用 Redis）里占用
OPENSANDBOX_POOL_MAX_TOTAL 个名额中的一个，再占用 OPENSANDBOX_CREATE_CONCURRENCY 个创建并发槽中的一个，
然后才调用控制器。每个名额用原子的“不存在才写入”占用，不存在全局锁，也不可能超发。
控制器只执行 Docker 操作。

- 不会因为别的会话正在创建而失败。交互式创建在事件循环上最多等待 OPENSANDBOX_ADMISSION_WAIT_S；
  预热从不等待，也不占用最后 OPENSANDBOX_USER_HEADROOM 个名额。
- 账本只在刚与控制器清单对账后才被信任：首次准入前、池维护至少每分钟一次、
  以及报告“已满”之前（须在该请求开始后完成）。对账释放已消失沙箱的名额，补登记无人持有名额的沙箱
  （控制器已应答但创建方崩溃），并按准入 token 处理结果未知的创建（超时、取消）。
- 任何不确定都保留名额而不是释放。删除后释放只是加速；漏掉释放的销毁路径
  只会让名额等到下一次对账，永远不会超发。
- `SandboxCapacityExhausted`（提示“沙箱资源已满”）表示刚完成的对账确认所有名额都在使用。
- 个人智能体电脑运行在独立控制器上，使用独立账本，名额由 PERSONAL_AGENT_SANDBOX_MAX_TOTAL 决定。

控制器进程内的 Docker 枚举统一容忍并发删除（`opensandbox_inventory.py` 把
`containers.list` 默认设为 `ignore_removed=True`），清单、启动恢复和失败清理
不会再因为枚举途中容器消失而 404。

领导进程从共享会话登记发现空闲会话，即使会话由其他 worker 创建或经历过重启，
也进入快照回收流程。失效登记按身份清理，不阻断其他会话。
快照任务随领导权停止；GC 只删除本次处理的 snapshot_id 对应记录。

输出读取不再触发挂载准备或对象存储目录写入。目录初始化发生在创建路径，
执行和文件访问仍核验权限。挂载探测超时或返回不确定错误时保留现有 FUSE helper，
只有确认缺失或断开时重建。命令观察失败先有限重试，再交给持久恢复；
没有可恢复引用的进程须确认停止，不能仅因观察失败就丢弃控制句柄。

backend 与控制器镜像一起发布：控制器不限制创建，只有所有 backend 都经过名额账本准入时容量约束才成立。

技能挂载必须配置 HOST_STORAGE_PATH，指向与后端 STORAGE_PATH 对应的宿主存储目录。缺失配置会明确失败，不再回退到源码目录。重启接管只识别显式归属标签，不根据旧启动命令推断归属。
