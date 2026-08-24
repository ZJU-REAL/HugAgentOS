# Harness Hook/Event 迁移与删除清单

中立契约位于 `core/harness`，禁止导入 AgentScope。`AgentScopeHookAdapter`
是唯一框架翻译层，负责把 reply/reasoning/acting 和框架事件转换成稳定
`Invocation`。事件只有 append 通道；只有 Hook 的显式 `Decision` 能修改、拒绝
或暂停执行。

迁移顺序与删除门槛由 `core.llm.middleware_migration.MIDDLEWARE_MIGRATION`
提供机器可读清单。每个旧 Middleware 已映射到明确 Hook seam，删除旧类前必须：

1. 中立策略只读取 `Invocation`，不导入 AgentScope 类型；
2. 修改字段属于该 stage 的白名单；
3. 原 Middleware 的行为与恢复测试在中立策略上达到同等覆盖；
4. 从 `agent_factory` 删除旧 Middleware，并把清单项改为 `deleted`。

当前阶段为 `adapter-covered`：运行时已由 Adapter 统一产生中立生命周期、事件
与 attempt 记账；旧策略不再作为 Agent 的并列 Middleware，而是仅在 Adapter 的
兼容链内运行，按清单逐项替换为中立 `HookSpec` 后删除，避免一次性重写十余个策略
造成行为漂移。无 `run_id` 的本地/测试入口也不再绕开 Adapter；有 durable run 的
每一次兼容策略执行都会形成 `kind=hook` 的独立 attempt。

可变字段只开放给运行时能够真正安全执行的修改：`transform_context` 的上下文输入、
压缩预算与替换结果。`before_model` 位于最终 ExecutionManifest 绑定之后，因此只读，
避免真实 provider 输入与证据清单不一致；`before_tool` 位于 AgentScope 的 schema 与权限
校验之后，因此同样只读，避免修改参数绕过安全检查。`after_model`、`after_tool` 和
`before_finish` 的输出也是只读观察值，仍可返回 reject/pause。压缩源 history 同样只读，
避免修改摘要输入后继续沿用原始 source hash/覆盖水位。
