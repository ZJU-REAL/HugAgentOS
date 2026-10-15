# 工作台来源与离线安装

同名本机和云端插件保留独立入口。入口显示“本机”或“云端”。选择入口后，启动、状态和资源请求固定到该来源。云端连接失败不会自动打开本机同名应用。

## 精确身份

目录返回 `origin: {kind, id}`。kind 为 local 或 cloud。id 是持久化的服务标识，不是地址或目录路径。插件 applicationKey 包含来源、服务标识、完整安装 ID 的 SHA-256、UTC 微秒安装时间和应用 ID。名称和 slug 不能代替安装身份。

启动返回相同 origin、installId、packageRevision 和 executionScope。工作台客户端使用 sourceRequest.workbenchRequest 发起作用域请求。仅包含 instanceId 的接口必须显式附带来源和 x-workbench-origin。插件 SDK 的 applicationSource(connection) 仅用于读取已绑定来源，不授予权限。

## 本机、云端与双模式

网页使用当前服务目录认证返回的来源；直接访问单机服务时，该来源可以是本机，但不能访问其他本机服务或原生接口。cloud_only 仅使用云端。local_only 使用本机。dual 同时读取两端目录。双模式本机执行面未确认身份时显示“本机执行面尚未就绪”。任一来源失败会单独提示。目录失败不等同于空目录。

应用声明的 availability.executionScopes 限制执行位置；requiresOnline 限制离线打开。页面不为其他来源重试。缺少某来源或权限时，恢复连接或重新登录后再重试该来源。

## 本机隐私

本机及双模式的导航偏好写入本机后端。云端偏好拒绝本机专有引用。偏好只携带 applicationKey，不携带页面定义、私有文件路径、凭据或文件内容。双模式设备偏好不自动上传至云端。

服务切换和账号切换清空旧目录缓存。备份恢复到原服务应保留 workbench_origin_identity；克隆到新服务或新设备应重新生成该标识。旧安装 key 的迁移必须使用已确认来源及准确安装记录，不得按名称匹配。

## 离线页面

SDK 必须打包进插件自身 JavaScript。HTML、脚本、CSS、图片和字体使用包内相对资源；不要依赖 CDN。宿主将资源嵌入隔离页面，并继续禁止网络连接、跨包路径和未打包 JavaScript 导入。缺少资源会返回明确的包错误。页面资源齐全不代表业务数据源可离线使用。

真实原生离线验收还应检查客户端内嵌运行时、有效本机登录、页面 ready、数据与文件、关闭回收和账号切换。
