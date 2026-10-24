# 自动化与任务循环

管理计划任务、执行记录、展示摘要、执行位置、通知投递与任务循环。`service.AutomationService` 保留 CRUD 和原子状态变更；`scheduling.compute_next_run` 保留时区感知的 Cron 计算；其余模块按执行绑定、投递目标、通知与远端副作用拆分。

对外接口包括原 `/v1/automations` 路由、MCP 任务服务调用的公开服务方法，以及 orchestration 调度器/恢复器使用的任务接口。路由顺序与权限键不变，调度器仍留在 orchestration。模型 `ScheduledTask`、`ScheduledTaskRun` 在 FULL 和 CE 均通过安装清单注册，表名与关系不变；执行状态新增 pending，升级脚本与本机 schema hook 同步更新状态约束。

依赖内核数据库、运行账本、项目权限、沙箱和临时状态接口。显式功能依赖为 **automation → desktop**：`tool_routing` 核验当前设备与账号授权，再选择本机任务执行；cloud 路径不使用此能力。当前 core.auth.desktop_bridge/core.capabilities 的物理路径待 P5 按清单迁移。

P5 边界待办：桌面工具执行器当前反向调用本模块的路由与副作用服务，属于原始导入基线中的循环债务；不能作为最终允许的反向依赖保留，应通过已注册的工具适配接口接入。本批不改变既有授权和幂等语义。

模块安装不自动启动调度器；现有 orchestration 启停和环境配置保持原行为。浏览器自动化属于另一套能力，不归入本目录。

执行记录的条件写入由 `runs.AutomationRunRepository` 管理。完成只接受 running 且 lease_token 匹配的记录。恢复先认领，再重读 chat_id。子执行通过事务内 on_accept 钩子关联并保存恢复参数，内核不导入功能模型。调度器取消只转交观察权，用户取消仍通过会话执行器处理。

接纳由 admission.admit 管理：条件更新消费触发信号并插入 pending 记录，同事务提交；禁止恢复独立清空触发信号或提前推进排期的旧路径。
