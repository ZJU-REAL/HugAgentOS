# 工作台错误码

[English](../../en/development/workbench-errors.md)

SDK 用错误码拒绝请求。HTTP API 保留宿主响应信封和 trace ID。
展示错误码及安全诊断编号，不展示票据、文件正文或上游凭据。
不得自动重试写操作。

| 错误码 | 含义 | 恢复 |
| --- | --- | --- |
| app_not_found | 应用不存在或不可发现 | 刷新认证后的目录 |
| app_disabled | 安装或定义已停用 | 联系管理员启用 |
| permission_denied / capability_denied | 实际鉴权拒绝 | 检查策略和已批准能力 |
| host_incompatible | 版本或必需能力不匹配 | 匹配 SDK、协议和 UI 版本 |
| origin_unavailable | 绑定的执行来源不可用 | 恢复同一准确来源 |
| revision_conflict | 并发写入改变了 revision | 读取最新内容，明确重新应用 |
| launch_expired | 短时启动描述已过期 | 重新申请已授权启动 |
| bridge_timeout | 握手或请求超时 | 提供有上限的明确重试 |
| session_closed | 宿主关闭或撤销连接 | 停止请求，打开新实例 |
| unknown_method | 不支持该方法 | 停用对应动作 |
| invalid_params / invalid_route | 参数无效或路由越界 | 修正字段，留在当前应用 |
| data_source_denied | 数据源未声明或未批准 | 检查 data_source:<id> 和 data.query |
| file_access_denied | 文件不可用或不归当前用户 | 重新选择自有文件 |
| cancelled | 用户或宿主取消操作 | 保留安全界面状态 |
| concurrency_limit / rate_limited | 超出请求预算 | 减少调用，使用有限重试 |
| message_too_large | 消息超过 256 KiB | 减少负载，使用受权文件引用 |

CLI 错误包含 structure、duplicate_id、duplicate_path、invalid_module、invalid_reference、
unsafe_url、incompatible_version、unsupported_capability、invalid_json、invalid_asset 和 ui_version。
CLI 诊断输出字段路径，不回显输入值。
CLI 成功只证明声明有效，不授予宿主访问权限。
应用键无效时不得按 slug 回退到其他安装。
