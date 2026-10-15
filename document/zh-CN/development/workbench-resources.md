# 工作台应用资源

UI v3 的 workspace 模块可声明包内 resource runtime。应用只可打开自身 target 模块的资源；模块须声明 resource.attach，通信还须声明 resource.command。宿主批准安装与当前用户权限仍是实际授权前提。

宿主为已打开实例装配 createWorkspaceResourceExtension，传入完整 applicationKey、instanceId、精确 installId/packageRevision、origin 和 executionScope。不要从插件消息中获取身份或选择另一个安装。SDK 的 resources(connection) 提供 open、renew、command、close；command 使用稳定操作 id。失败后不自动重放有副作用操作。

宿主在创建资源后每 25 秒续租。未打开的应用不启动计时器、iframe 或进程。关闭、撤销、退出和保活预算淘汰必须 dispose；实例 signal abort 立即停止计时器，随后关闭资源。租约 90 秒到期后不允许续活。关闭失败保留 closing，由已有 janitor 重试，并继续计入配额。

资源复用 /v1/plugin-resources 的票据、资产与流服务。新生命周期接口在 /v1/workbench/applications/{key}/instances/{instance}/resources。创建需 Idempotency-Key；同一 key 的重试返回同一资源。资源关闭后相同 key 返回 410，不能自动重启。跨用户、安装、版本、实例或实际执行面不允许访问。

数据库扩展 plugin_resources，chat_id 与 application_key+instance_id 互斥，不建立虚假聊天。升级保留原会话资源。降级遇到应用资源记录时拒绝执行；管理员须先按获准的备份与导出流程处理，不自动丢弃数据。桌面 SQLite 使用启动升级，服务部署使用 Alembic；在副本验证后再迁移真实数据。

真实 provider、PostgreSQL、浏览器和原生设备验收须独立记录。模拟边界测试不能替代实际隔离与回收验证。
