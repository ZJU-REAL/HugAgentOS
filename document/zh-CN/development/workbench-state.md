# 工作台界面状态与有限保活

插件应用在 lifecycle.stateSchemaVersion 声明状态格式版本。宿主才会公布 state.load、state.save。没有声明的页面可设置 dirty，但不能宣称重载后恢复草稿。状态用于筛选、路由和界面选项，不存密码、Cookie、Token、私钥或文件正文。

SDK 的 applicationState(connection) 提供 load、save、setDirty。应用先 load，再按返回的 revision 保存。save 不会自动清除 dirty；业务保存成功后，应用主动 setDirty(false)。resetRequired 为 true 时，payload 不返回旧内容。应用须先向用户说明并取得重置选择，再提交 reset:true。不能把旧格式当成新格式读取。

HTTP 为 GET/PUT /v1/workbench/me/application-state/{applicationKey}。查询包含 workspace、packageRevision；插件还必须提供准确 installId、origin 和 executionScope。instanceId 使用宿主实例池生成的同一 ID，供资产和资源生命周期关联。用户身份来自登录上下文，不能提交 userId。

PUT 请求为 {revision,schemaVersion,payload,reset?}。返回 {revision,schemaVersion,payload,resetRequired}。并发 revision 不匹配返回 409。状态格式变化未显式重置返回 409；容量不足返回 413；凭据字段返回 422。错误不回显状态内容。所有状态响应禁止缓存。

状态按用户、应用、安装、来源、执行位置和工作区隔离。应用升级保留稳定安装身份；格式版本变化须显式重置。状态接口只接受当前安装版本；保留旧资产租约不会授权旧版本覆盖新状态。应用停用或撤销后，每次状态调用重新鉴权并拒绝访问。

默认每应用所有工作区合计 256 KiB，每用户合计 10 MiB。WORKBENCH_STATE_APP_BYTES 和 WORKBENCH_STATE_USER_BYTES 可调低；上限分别为 1 MiB 和 100 MiB。配额包含 JSON UTF-8 字节，CAS 与合计配额在同一事务内检查。状态逻辑实体复用 ContentBlock 的独立 workbench_application_state 命名空间，无新增数据库迁移。

宿主只在用户打开后初始化实例。相同安装和工作区重复打开复用同一实例，默认保留当前实例与两个非活动实例。超额按 LRU 释放。未保存页面切换或关闭时，由宿主显示放弃更改确认；用户可取消，并返回页面保存。dirty 不能无限占用预算，也不能阻止退出、撤销或卸载清理。

WorkspaceInstances 必须跨应用路由切换保持挂载。完整账号和后端来源变化时，更换 ownerScope，清理旧实例。调用方将 state extension 与导航、资源 extension 组合后交给桥接器；将 ready/failed 接入实例状态机。释放时同步中止 entry.signal，移除 iframe，并关闭同一 entry.instanceId 的端口、资源及资产租约。请求重试只重建当前实例，不重放业务写操作。
