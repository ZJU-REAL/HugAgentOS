# 评测执行隔离

提供 eval_ 会话的受限执行上下文、工具和独立沙箱租约。对外保留评测沙箱 API、admission 与 sandboxes 服务；runtime 管理执行范围，sandbox_provider 管理租约授权、进程和容器操作。

`extension.setup` 通过现有 register_sandbox_route 注册 eval_ 前缀。每次选择提供方都核对 OpenSandbox 配置，禁止回退到普通沙箱；扩展未注册时保留拒绝策略。FULL 与 CE 均安装本模块。租约使用已有临时状态接口，无新增 ORM 表。

依赖内核的沙箱协议、临时状态、配置、工具和运行取消接口；没有向其他 features 模块的依赖。直接运行取消引用按计划 P6c 的取消注册点处理，当前批次保持原行为。
