# 普通外部应用

管理员在系统配置台的「应用配置 → 普通外部应用」新增、编辑和启停外链（管理台属于商业版 EE）。用户在应用中心的「外部应用」发现入口，点击后在浏览器新窗口打开。应用自行登录，宿主不传递 SSO token、Cookie 或其他登录凭据。

声明复用独立工作台协议包，当前管理端只接受 `schemaVersion`、`id`、`title`、`description` 和 `target`。目标必须为 `{kind:"external", mode:"browser", url:"https://example.org/"}`。声明不接受认证配置、回调配置、嵌入或插件字段；认证通过独立接口配置，见[外部应用认证](workbench-external-auth.md)。ID 创建后不可修改，名称可修改。URL 只允许 HTTP/HTTPS，拒绝用户信息、无效主机、反斜线、控制字符和常见凭据参数。浏览器打开不触发服务端抓取。

接口均位于 `/v1/workbench`：

| 接口 | 行为 |
|---|---|
| GET /applications | 当前身份有权发现且启用的普通外部应用 |
| GET /applications/{key} | 已授权的定义和状态 |
| POST /applications/{key}/launch | 每次重新检查权限及启用状态，返回安全打开目标 |
| GET/POST /admin/external-applications | Config 管理权限读取/创建 |
| PUT /admin/external-applications/{key} | 携带当前 revision 更新，冲突返回 409 |
| GET /admin/audit | Config 管理权限读取最近 100 条操作审计 |

请求格式为 `{"declaration":{...},"enabled":true}`，更新还需 `revision`。applicationKey 为 `external:<id>`。所有接口保留标准响应信封。权限复用已有 allowed_apps 的个人、角色、团队及系统解析；普通用户无法通过 user_id 参数改变身份。未授权资源统一返回 404，已停用资源 launch 返回 409。失败页面提供反馈和重试入口，弹窗失败不会无响应。

定义及操作审计分别保存在既有 ContentBlock 的 `workbench_external:` 和 `workbench_audit:` 键下。同一次更新的 revision、定义与审计在同一事务提交，无新增数据库表或历史数据迁移。备份和恢复使用完整数据库快照；旧内容导出仅包含旧内容块，不包含工作台定义。升级前保存数据库快照；恢复旧代码前先冻结管理写入并保存新增数据，避免丢失增量。

旧 app_config 定义保留，不自动迁移。通用 SSO Token 拼接已移除，未配置旧入口显示认证诊断。逐项审核、显式适配、退出和恢复流程见[外部应用认证](workbench-external-auth.md)。新定义仍拒绝与旧应用或内置 ID 冲突。

验证包含隔离真实 HTTP/SQLite、真实浏览器点击和进程重启，以及隔离 PostgreSQL 测试。HTTP 浏览器夹具使用 mock 身份模式；正式会话及原生桌面验证仍需相应环境，不能把夹具结果当作生产登录或原生客户端通过。

# 普通外部应用

管理员在系统配置台的「应用配置 → 普通外部应用」新增、编辑和启停外链（管理台属于商业版 EE）。用户在应用中心的「外部应用」发现入口，点击后在浏览器新窗口打开。应用自行登录，宿主不传递 SSO token、Cookie 或其他登录凭据。

声明复用独立工作台协议包，当前管理端只接受 `schemaVersion`、`id`、`title`、`description` 和 `target`。浏览器目标为 `{kind:"external", mode:"browser", url:"https://example.org/"}`。声明不接受认证配置、回调配置或插件字段。显式嵌入模式及单独的 embedding 管理字段见[外部页面嵌入](workbench-external-embedding.md)。ID 创建后不可修改，名称可修改。URL 只允许 HTTP/HTTPS，拒绝用户信息、无效主机、反斜线、控制字符和常见凭据参数。浏览器打开不触发服务端抓取。

接口均位于 `/v1/workbench`：

| 接口 | 行为 |
|---|---|
| GET /applications | 当前身份有权发现且启用的普通外部应用 |
| GET /applications/{key} | 已授权的定义和状态 |
| POST /applications/{key}/launch | 每次重新检查权限及启用状态，返回安全打开目标 |
| GET/POST /admin/external-applications | Config 管理权限读取/创建 |
| PUT /admin/external-applications/{key} | 携带当前 revision 更新，冲突返回 409 |
| GET /admin/audit | Config 管理权限读取最近 100 条操作审计 |

请求格式为 `{"declaration":{...},"enabled":true}`，更新还需 `revision`。applicationKey 为 `external:<id>`。所有接口保留标准响应信封。权限复用已有 allowed_apps 的个人、角色、团队及系统解析；普通用户无法通过 user_id 参数改变身份。未授权资源统一返回 404，已停用资源 launch 返回 409。失败页面提供反馈和重试入口，弹窗失败不会无响应。

定义及操作审计分别保存在既有 ContentBlock 的 `workbench_external:` 和 `workbench_audit:` 键下。同一次更新的 revision、定义与审计在同一事务提交，无新增数据库表或历史数据迁移。备份和恢复使用完整数据库快照；旧内容导出仅包含旧内容块，不包含工作台定义。升级前保存数据库快照；恢复旧代码前先冻结管理写入并保存新增数据，避免丢失增量。

旧 app_config 及其登录适配继续保留，未复制或自动迁移为新定义。普通新增外链使用上面的管理区域。新定义拒绝与旧应用或内置应用 ID 冲突。旧入口的认证适配与最终迁移另有版本任务，不把旧 SSO 行为推给新外链。

验证包含隔离真实 HTTP/SQLite、真实浏览器点击和进程重启，以及隔离 PostgreSQL 测试。HTTP 浏览器夹具使用 mock 身份模式；正式会话及原生桌面验证仍需相应环境，不能把夹具结果当作生产登录或原生客户端通过。
