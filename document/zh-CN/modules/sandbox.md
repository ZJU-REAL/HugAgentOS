# 沙箱执行系统

> 最后更新：2026-06-11

沙箱是 HugAgentOS 中智能体执行代码的隔离环境：模型在对话里调用 `bash` 跑命令、运行[技能](agent-skills.md)脚本、生成可下载产物，全部发生在沙箱里而非后端主进程。系统通过统一的 **Provider 协议**抽象出三种可切换的执行后端——从单机轻量的 script_runner 到带持久会话、快照恢复的 OpenSandbox，再到远端 MicroVM 集群的 Cube——上层工具代码对此完全无感。

按[版本划分](../editions/overview.md)：**轻量沙箱（script_runner）+ 沙箱工具/offload 基础设施属社区版 CE**；**持久沙箱（opensandbox / cube，会话保持、环境复用、快照恢复）属商业版 EE**——社区版派生树剔除这两个 provider 文件，工厂自动回退轻量实现。

## Provider 协议（core/sandbox/protocol.py）

所有 provider 实现同一个 `SandboxProvider` Protocol，字段契约与 script-runner sidecar 的 HTTP 接口逐一对齐：

| 方法 | 职责 |
|---|---|
| `execute(req: ExecuteRequest) -> ExecuteResult` | 执行脚本/命令，返回 stdout/stderr/exit_code/耗时/产物文件 |
| `stage_files(user_id, files)` | 把输入文件暂存到用户 myspace 缓存，返回沙箱内可引用的绝对路径 |
| `put_file(session_id, path, content)` | 写字节进沙箱指定路径（自动建上级目录） |
| `get_file(session_id, path)` | 从沙箱读文件字节 |
| `current_sandbox_id(session_id)` | 纯查询当前绑定的底层沙箱身份（用于检测沙箱被重建） |
| `health()` | 健康探测 |
| `admin_*` 系列 | 安全管理台只读视图（能力声明 / 实例枚举 / 单实例详情 / 池统计），不支持的能力抛 `SandboxAdminNotSupported` 由 UI 置灰 |

`ExecuteRequest` 的两个关键可选字段：

- **`session_id`**：持久型 provider 用它把多次 `execute` 绑定到同一个底层沙箱实例（= 跨调用保留变量、pip 包、/workspace 文件）；ephemeral provider 忽略；
- **`user_id`**：触发 myspace 文件可见性（bind-mount 或 seed），见下文 Plan F。

## 三个 Provider 实现

| Provider | 文件 | 形态 | 适用场景 | 版本 |
|---|---|---|---|---|
| `script_runner` | `script_runner_provider.py` | 包装 `hugagent-script-runner` sidecar 容器的 HTTP 调用（容器内 setrlimit 子进程），无状态 | 单机部署、一次性代码执行，默认值 | CE |
| `opensandbox` | `opensandbox_provider.py` + `_opensandbox_*.py` | 阿里 OpenSandbox（Docker 容器 + Jupyter 持久 kernel），per-chat 持久会话 + 预热池 + 快照 | 多轮迭代分析、技能重工作流 | **EE** |
| `cube` | `cube_provider.py` | 腾讯 CubeSandbox（E2B 兼容 MicroVM），**远端节点**——后端通过 `e2b_code_interpreter` SDK 跨网访问，无本地 sidecar | 后端宿主机资源紧张 / 需要强隔离（MicroVM 级）/ 沙箱算力独立扩容的部署 | **EE** |

切换由环境变量 `SANDBOX_PROVIDER` 控制（`core/sandbox/factory.py` 单例工厂）。CE 树中 `opensandbox` / `cube` 模块不存在时，工厂打告警并**自动回退 script_runner**，配置不变也能跑通。

Cube 的设计取舍（远端节点版的代价）：所有语言统一走"写脚本文件 + `commands.run`"，不依赖 Jupyter；**无 host bind-mount**（myspace 文件由工具层经 `put_file` 物化、技能文件运行时按需推送，`CUBE_SKILL_PREPUSH` 控制预推）；无快照体系；`session_id` 仍绑定持久 MicroVM（首次 create、后续 connect 复用）。

## Agent 侧三件工具

`core/llm/tools/sandbox_tool.py` 向智能体注册三个工具（`agent_factory` 在 Phase 3.5 调用各 `register_*`）：

| 工具 | 作用 |
|---|---|
| `bash(command, timeout)` | 在沙箱执行任意 shell 命令；工作目录 `/workspace`，同会话内文件持久；硬上限 120 秒；另注册大写 `Bash` 别名（兼容部分模型按训练惯例发出的大写工具名调用） |
| `sandbox_put_artifact(artifact_id, dest_path)` | 把平台 artifact（用户上传文件、图表工具产物等）的字节拷入沙箱路径——沙箱不会自动看到上传文件 |
| `sandbox_get_artifact(src_path)` | 把沙箱内文件登记为可下载 artifact——bash 产物不会自动出现在附件区 |

沙箱会话标识由 `resolve_sandbox_session(sandbox_session_id, chat_id)` 解析：主对话/计划执行 → `chat_id`（per-chat 持久 kernel）；批量项/子智能体 → `""`（ephemeral）。

**MySpace 回写闭环**：`bash` 命令成功且命令串包含 `myspace` 时，`_sync_myspace_changes` 列出沙箱 `/workspace/myspace/{uid}` 下近 10 分钟修改的文件、与后端镜像缓存做 md5 比对，差异文件逐个过用户确认门（`MYSPACE_WRITE_CONFIRM`，非交互模式直接拒写）后以**同 file_id 就地回写**「我的空间」——下载/预览链接不变。这闭合了"模型用 python-docx 在沙箱改了 docx、用户空间却纹丝不动"的断链。

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
- **TTL 与续期**：沙箱服务端 TTL 默认 1800s（`OPENSANDBOX_DEFAULT_TIMEOUT_S`）；每次会话活动触发限频（60s）的后台 renew，不阻塞请求路径；renew 失败区分 lifecycle 信号（立即标 stale）与瞬时网络错误（连续 3 次才升级）；
- **双层暖池**：进程启动即预热通用双桶池；Plan F 开启后 user-bound 流量改走 per-user 的 `_JupyterUserPool`（避免挂了别人 myspace volume 的沙盒被串用），idle reaper（600s，`OPENSANDBOX_IDLE_REAP_S`）把空闲会话的沙盒**洗净 kernel 后回 user idle 池**复用而非销毁。

## 快照持久化（EE）

完整设计见 [sandbox-snapshot-design.md](../../sandbox-snapshot-design.md)。目标：会话空闲时不白白占着 Docker 资源，又能在用户回来时**带着文件系统**满血复活。

- **park（雪藏）**：后台 worker 每 60s 扫描，对 idle 超过 `OPENSANDBOX_IDLE_SNAPSHOT_THRESHOLD_S`（默认 1500s）的会话发起 snapshot（实测 accept→Ready 约 60s，docker commit），Ready 后 upsert `chat_sandbox_snapshots` 表（chat_id 为主键）、销毁容器；单轮最多并发 park 3 个保护 docker daemon；
- **restore（恢复）**：`_create_session_for` 优先查 Q2 user idle 池（~7s 暖路径），未命中再查 DB snapshot（~15–20s restore，仍快于全新创建），都没有才全新建。从 snapshot 启动时 volumes 必须**重新声明**——docker commit 不保存 mount 配置，否则 `/workspace/skills/` 等 bind-mount 会丢；
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

所有技能文件通过**单一只读 bind mount** 暴露在沙箱 `/workspace/skills/<id>`（`_make_skills_volume`）：挂载源是统一技能目录 `$HOST_STORAGE_PATH/sandbox_skills`（内置技能启动时同步进来、DB 技能按需物化，见[技能系统](agent-skills.md)）。read-only 保证沙箱内不可篡改技能；目录 bind 是实时的，新导入技能立即可见。`HOST_STORAGE_PATH` 未配置时退回只挂内置源码树并告警。

## 超长工具结果 offload

`core/llm/offloader.py::SandboxOffloader` 实现 AgentScope 2.0 的 `Offloader` 协议：上下文压缩 / 工具结果截断时，溢出内容不再被静默丢弃，而是经 `put_file` 写入沙箱隐藏目录 **`/workspace/.offload/`**（`tool_<call_id>.txt` / `context_<hex>.txt`），路径拼进给模型的 system-reminder——模型随后可用 `Read` 或 `bash(cat/grep …)` 按需回查全文。协议要求方法**绝不抛异常**（写失败返回降级说明），且仅在沙箱工具启用时挂载。

## 管理员沙箱管理

- **只读监控（安全管理台）**：`api/routes/v1/config_security.py` 暴露 `/v1/config/security/sandbox/*`——总览、实例列表、单实例详情、快照列表、重建历史、生效配置；底层走 provider 的 `admin_*` 接口，按 `admin_capabilities()` 声明裁剪 UI（script_runner 无法枚举实例即置灰对应列）。
- **依赖重建（商业版 EE）**：`api/routes/v1/admin_sandbox.py`（`/v1/admin/sandbox/*`）聚合所有技能声明的 pip/apt 依赖（`core/services/skill_deps_aggregator.py`），管理员一键触发沙箱镜像重建：`script-runner` / `opensandbox` 目标走本地 `docker compose build`，`cube` 目标走 SSH 到远端节点重建模板并热切（`core/services/sandbox_rebuild_service.py` + `cube_template_builder`），可查看每次 run 的状态与日志。技能新依赖从此"烤入"镜像，无需手工改 Dockerfile。

## 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SANDBOX_PROVIDER` | `script_runner` | provider 选择：`script_runner` / `opensandbox` / `cube` |
| `SANDBOX_RUNNER_URL` | `http://hugagent-script-runner:8900` | script_runner sidecar 地址 |
| `OPENSANDBOX_DOMAIN` / `OPENSANDBOX_API_KEY` / `OPENSANDBOX_IMAGE` | — | OpenSandbox 服务端与镜像 |
| `OPENSANDBOX_DEFAULT_TIMEOUT_S` | 1800 | 沙箱服务端 TTL |
| `OPENSANDBOX_POOL_{JUPYTER,LIGHT}_{MIN,MAX}_IDLE` / `OPENSANDBOX_POOL_MAX_TOTAL` | 2/3、2/5、20 | 预热池水位 |
| `OPENSANDBOX_IDLE_REAP_S` | 600 | 空闲会话回收（回 idle 池）阈值 |
| `OPENSANDBOX_SNAPSHOT_ENABLED` | true | 快照体系总开关 |
| `OPENSANDBOX_IDLE_SNAPSHOT_THRESHOLD_S` | 1500 | idle 多久触发 park |
| `OPENSANDBOX_SNAPSHOT_RETENTION_DAYS` | 7 | 快照保留天数 |
| `OPENSANDBOX_SNAPSHOT_WAIT_TIMEOUT_S` | 120 | 等 snapshot Ready 的轮询上限 |
| `OPENSANDBOX_MYSPACE_BIND_MOUNT_ENABLED` | true | Plan F myspace 直挂开关 |
| `HOST_STORAGE_PATH` | — | 宿主机 storage 真实路径（bind-mount 源） |
| `SANDBOX_SKILLS_DIR` | `$STORAGE_PATH/sandbox_skills` | 统一技能目录覆盖 |
| `MYSPACE_WRITE_CONFIRM` | true | /myspace 写操作用户确认硬保险 |
| `CUBE_API_URL` / `CUBE_API_KEY` / `CUBE_TEMPLATE` / `CUBE_API_SANDBOX_DOMAIN` | — | Cube 节点接入 |
| `CUBE_IDLE_REAP_S` / `CUBE_POOL_MIN_IDLE` / `CUBE_OWNER_TAG` | 600 / 2 / — | Cube 回收、预热、多环境共用节点时的归属标签 |
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
| `src/backend/core/llm/tools/sandbox_tool.py` | bash / sandbox_put_artifact / sandbox_get_artifact |
| `src/backend/core/llm/offloader.py` | 超长结果落盘 /workspace/.offload |
| `src/backend/api/routes/v1/admin_sandbox.py` | 依赖重建管理 API（EE） |
| `src/backend/api/routes/v1/config_security.py` | 安全管理台沙箱只读视图 |
| `src/backend/core/services/sandbox_rebuild_service.py` | 镜像/模板重建编排（EE） |
| `docker/Dockerfile.script-runner` / `docker/Dockerfile.opensandbox` / `docker/Dockerfile.cube-sandbox` | 三种沙箱镜像 |

相关文档：[技能系统](agent-skills.md) · [MCP 工具系统](mcp-tools.md) · [个人空间](projects-myspace.md) · [版本与许可](../editions/overview.md)
