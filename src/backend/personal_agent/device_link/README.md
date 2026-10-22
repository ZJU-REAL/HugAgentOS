本机设备链接：根据用户同意轮询个人智能体设备邮箱、执行本机操作并回传收据。保留账号隔离、许可复核与至多一次执行语义，不新增监听端口。

模块归 personal_agent 所有，由原 API 生命周期的 execution-plane singleton 入口管理；不依赖云端 PERSONAL_AGENT_ENABLED 开关。桌面链接功能即使未启用云端个人智能体也能连接用户已授权的云端。独立 driver 仍由 Dockerfile.personal-agent 复制到容器原路径。无新增数据库模型，不单独加入 INSTALLED_FEATURES。
