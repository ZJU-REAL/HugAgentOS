# 源码版容器云部署：外部服务与真实空间挂载（商业版 EE）

HugAgentOS 源码支持外部 PostgreSQL、Redis、MinIO，以及 Kubernetes OpenSandbox。部署清单和精简镜像交付入口由 `deploy` 分支的 `deploy_kit/kubernetes/external.py` 维护，完整步骤见该分支同名部署文档。不要使用全栈 Compose 清单在受限平台创建数据库或存储卷。

## 配置和运行

使用 deploy 分支的 external init/render/preflight 流程生成配置，将源码构建的 backend、mcp、frontend 镜像 digest 填入 site.json；沙箱 server 和 space-mount 使用同套经 patch_upstream 校验的版本。不可只替换未经适配的上游 server 镜像。

容器通过 Kubernetes ConfigMap/Secret 的 envFrom 获得环境变量，不需要镜像内的 .env。设置 PYTHON_DOTENV_DISABLED=1；真实凭据不进入源码或镜像。主要配置是 DATABASE_URL、REDIS_URL、S3 端点/桶/凭据、DEPLOY_PROFILE=kubernetes、STORAGE_TYPE=s3、SESSION_STORE=redis、MEM0_VECTOR_PROVIDER=pgvector、HUGAGENT_SPACE_MOUNT_MODE=sidecar，以及所需模型端点。PERSONAL_AGENT_ENABLED=false、MODEL_GATEWAY_ENABLED=false；本部署不包含知识库组件或模型网关。使用 external 渲染器生成完整变量，避免只复制此处摘要漏项。

secrets.json 中 PostgreSQL 和 Redis 按管理员交回的字段分别填写：DB_HOST、DB_PORT、DB_NAME、DB_USER、DB_PASSWORD，REDIS_HOST、REDIS_PORT、REDIS_PASSWORD、REDIS_DB；按需加 DB_SSLMODE（disable/allow/prefer/require；需要 CA 证书的校验模式暂不支持）、REDIS_TLS（true/false）、REDIS_USERNAME。渲染器对特殊字符编码后生成 DATABASE_URL、REDIS_URL，密码无需手工转义；直接填写完整 URL 仍然兼容。应用不给 Redis 键加前缀，必须使用独立库号，不能与其他系统共用同一个库号。

单个已有 PostgreSQL 数据库承载业务表、许可证状态和 pgvector 记忆（启动时自动建表和按用户检索所需的索引）；需要预装 vector 扩展并允许 owner 执行迁移。MinIO 保存文件、artifact 元数据和公开手册/品牌资源。更新 Secret/ConfigMap 后重新渲染并滚动替换 Pod，不在容器内手改配置。

许可证存于 PostgreSQL。每个进程每 LICENSE_DB_REFRESH_SECONDS（默认 30 秒）重新读取一次，请求本身不访问数据库；数据库短暂不可用时沿用最近一次确认的结果，至多 LICENSE_DB_OUTAGE_GRACE_SECONDS（默认 300 秒），冷启动或更长的中断仍拒绝放行。

每个 session 的沙箱临时目录独立。同一用户的“我的空间”通过真实 FUSE 挂载共享持久文件；用户和团队空间授权沿用现有权限检查。挂载丢失时拒绝执行，不降级成下载副本。技能在每次命令前按当前完整授权视图核对，视图有变化才重新下发，沙箱复用给新会话时也重新下发，保留执行权限；相对路径可直接使用，禁用、删除或撤权的包从当前技能目录清除，未变目录和文件保持不动。撤权不追溯删除用户之前复制到其他位置的数据。Kubernetes 使用云端技能来源，不支持混用桌面 capability publication。Kubernetes 不支持现有宿主机第三方凭据目录挂载；检测到这种配置会明确报错，相关第三方集成未纳入本次部署验收。

## 平台必须实际通过的条件

需要 Kubernetes 1.29+、OpenSandbox CRD/namespace 管理权限、有效的 NetworkPolicy、受控 helper 的 privileged/FUSE/Bidirectional mountPropagation。平台禁止特权时设 `HUGAGENT_SPACE_MOUNT_MODE=sync`（deploy 渲染器 `space_transport: "sync"`）：不挂载，backend 在命令前后经沙箱 HTTP 文件接口增量同步空间文件，不需要 privileged/FUSE，一致性降为命令边界一致，见 `internal design docs`。运行代码的主容器保持非特权，MinIO 凭据只进入 helper。没有持久卷并不等于不需要这些挂载权限；若平台只有普通 Pod 权限且禁止 helper 能力，就无法满足真实挂载要求。

先运行 external preflight，真实检查 PG/Redis/MinIO、FUSE 读写/只读/凭据隔离、会话 NetworkPolicy，再执行迁移和应用上线。生产平台内网目前不可访问，权限未确认；本地验证不能代替目标平台预检。

## 本次本地验证

已在隔离 Kubernetes 环境验证两个分支的真实 PG 授权→产品 Bash→沙箱执行、会话临时文件隔离、共享个人空间和复用检查。源码版额外验证真实数据库私有技能发布/执行，以及拒绝访问其他用户私有技能。还验证挂载 helper/控制面重启、故障拒写、恢复、只读空间和非特权执行容器。

这些验证不等同于目标平台已部署，也未验证目标模型端点的真实回复。上线前仍需在目标环境完成登录、模型回复、两个 session 执行代码、文件持久化与隔离验收。代码未自动提交或推送。
