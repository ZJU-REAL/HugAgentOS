# CLI ACP 接入

HugAgentOS CLI 提供原生 Agent Client Protocol（ACP）v1 stdio 入口。实现位于产品 CLI 和后端，不依赖评测代码。Node SDK 2.0.2 的默认协议是 v1。

## 安装与启动

先按[单机安装说明](../deployment/quick-install.md)初始化本机账号及模型，再从仓库构建 CLI：

```bash
(cd packages/chat-sdk && npm ci && npm run build)
(cd packages/cli && npm ci && npm run build)
npm install -g ./packages/cli
hugagent --mode local --output acp
```

需要 Node.js 20+、Python 3.11+ 和可运行的 `hugagent-server`。也可设置 `HUGAGENT_REPO` 与 `HUGAGENT_PYTHON` 指向源码和 Python 环境。`HUGAGENT_HOME` 默认 `~/.hugagent`，账号、模型与本机引擎沿用现有配置。initialize 不启动引擎；session/new 才连接或启动本机引擎。

ACP 客户端启动命令数组为 `["hugagent", "--mode", "local", "--output", "acp"]`，通过 stdin/stdout 收发 JSON-RPC；stderr 为诊断。不要同时传 `-p`、prompt、resume、continue 或 `--auto-approve`。当前正式支持本机模式，远程 cloud/device 服务不支持本机租约接口。

## 能力与边界

| 功能 | 行为 |
| --- | --- |
| 会话 | new/load/resume/list/close/delete；load 分页回放全部历史，resume 不回放 |
| 对话 | 多轮、多会话；同会话禁止重叠 prompt；流式文本、思考、工具、计划、用量 |
| 配置 | model 与 mode 会话隔离；set_mode 与 set_config_option 同步通知 |
| 权限 | 默认 ask；allow_once / reject_once；迟到授权不会恢复已取消动作 |
| 用户问题 | 客户端支持 form elicitation 时询问；否则显式跳过，不能代用户回答 |
| 客户端文件 | 协商 fs 后通过 client_read_text_file / client_write_text_file 调用客户端 |
| 客户端终端 | 协商 terminal 后 create/wait/output；取消时 kill/release，清理等待有上限 |
| MCP | session/new/load/resume 传入 stdio、HTTP、SSE；会话级临时注入，不写全局配置 |
| 内容 | 文本、资源链接、内嵌文本、图像/二进制附件；附件走上传接口，不把 base64 当提示词 |
| 压缩 | `/compact` 调用引擎压缩能力 |
| 未提供 | 音频输入、additionalDirectories、ACP 内交互式登录、持久 allow_always |

图片是否能被理解还取决于所选模型；每项附件最大 10 MiB、每轮最多 10 个。资源链接仅作为引用，不自动下载 URL。cwd 必须为本机存在的绝对目录，会话只能连接其所属工作区。

ask 复用引擎权限规则，并非每个工作区读写都弹窗；客户端提供的 MCP 工具须经过权限检查。auto 使用引擎自动策略。full 不确认工具调用，命令没有 OS 沙箱约束，只应在用户明确选择时使用：

```bash
hugagent --mode local --output acp --permission-mode full
```

容器评测需要全自动执行时，可由外层沙箱提供隔离并明确选择 full。ageval 应直接使用上述原生命令，不再安装或调用 `evaluation/cli_acp` 适配器。

## 生命周期和故障

客户端配置、MCP 凭据与授权策略绑定临时租约。心跳续租不更改模式；每轮冻结受理时的权限，后续配置不能升级正在运行的轮次。取消、EOF 或关闭会话会取消所属 run 并释放租约，不停止其他客户端共享的引擎。

若提交已发出但 run 回执丢失，连接会撤销该会话并要求重新创建，避免后台无人管理的执行。后端取消失败时保留不可用租约占位，阻止回退到全局权限；应先确认后台任务已经停止，再重启本机引擎恢复占位。客户端终端清理超时会在 stderr 报告，终端最终清理由客户端负责。

stdout 错误不返回底层日志或凭据；MCP URL 查询参数及认证内容在日志输出边界脱敏。认证未就绪时先运行本机 onboard。协议请求成功与解题成功不同：应独立检查实际文件和测试结果。

## 开发与验证

产品模块：`packages/cli/src/acp/`、`packages/cli/src/modes/acp.ts`；后端租约与接口：`integrations/agent_client/`（安装模块，启动时注册路由和租约实现）；core 只通过 `core/harness/registry/client_sessions.py` 端口访问租约。

```bash
(cd packages/cli && npm test)
PYTHONPATH=src/backend .venv/bin/pytest src/backend/tests/integrations/test_client_session*.py -q
```

`packages/cli/test/integration/acp-live.mjs` 是隔离容器真实模型验收；`acp-installed.mjs` 调用 PATH 上实际安装的 CLI，修复订单汇总程序并执行独立断言。二者均显式 opt-in，不使用用户业务工作区。协议单测、真实模型验证与 WorkBuddy 独立评分应分别记录。
