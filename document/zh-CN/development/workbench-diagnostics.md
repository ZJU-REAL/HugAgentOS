# 工作台应用诊断

用户加载失败时可以复制诊断编号并点击重试。重试重新检查当前安装和访问权限，不自动重放业务写操作。浏览器无法写入剪贴板时，可以选择页面中的编号手动复制。账号切换后旧编号和未完成请求不会更新新账号界面。

系统配置管理台的应用配置页提供“应用诊断”（商业版 EE）。管理员可以查看来源、版本、安装摘要、执行面、启停、认证类型和最近失败。输入真实用户 ID 后，检查复用工作台目录的实际权限逻辑。未选择用户时显示 requires_user_context，不把未知权限标成已授权。

配置预检查复用保存外部应用时的协议与语义验证，不保存配置。认证策略继续由外部认证配置接口管理。认证诊断只显示 independent、shared_identity、exchange 或 host_session，不返回秘密、目标 URL、登录回调或认证配置引用。

## HTTP 接口

全部路径位于 /v1/workbench。API 使用现有信封格式。

| 接口 | 权限与行为 |
| --- | --- |
| GET /admin/diagnostics | 配置管理员；可选 user_id 检查真实用户的运行条件 |
| POST /admin/diagnostics/preview | 配置管理员；JSON declaration；使用真实外部声明验证，不写入 |
| POST /admin/diagnostics/probe/{key} | 配置管理员；只探测已经保存的外部应用目标 |
| GET /me/diagnostics | 当前用户自己的最近事件 |
| GET /me/diagnostics/{id} | 仅记录所有者；他人和不存在编号均返回 404 |
| POST /applications/{key}/diagnostics | 当前可发现应用；仅接收 page_failed 或 bridge_timeout |

服务端启动失败会附带 diagnosticId。不存在或无权发现应用的失败记录不保存请求中的应用名称。浏览器失败记录来自用户报告，不能当作服务器已确认第三方崩溃的证据。

## 安全探测

探测只发送无凭据 HEAD。目标必须为 HTTPS 443，不包含查询参数或片段。全部 DNS 地址必须为公网地址；实际连接使用校验后的数字地址，保留 TLS 主机名校验，并检查真实对端。拒绝任何重定向，不跟随 Location，不读取响应正文，不使用环境代理。DNS 等待上限 2 秒，同时最多两个解析任务；每次连接上限 2 秒，最多尝试四个地址。HTTP 标准库另有单行头部及头部数量上限。HEAD 可达不表示第三方登录、嵌入或业务操作已经通过。

私网应用仍可按原浏览器打开规则配置。诊断服务器拒绝主动探测私网，不改变浏览器打开的原有行为。

## 日志与指标

诊断白名单包含 appKey、instanceId、installId、revision、scope、traceId、事件和错误码。用户、安装和实例标识使用摘要；日志不写正文、文件内容、URL、票据、密钥或异常文本。保留最近约 1000 个事件，指标明示该保留窗口，不作为累计计费统计。

指标包含目录耗时、启动成功率、握手超时、权限拒绝、资源配额拒绝、释放失败和活跃资源实例。活跃资源实例按真实 application owner 的有效租约计数，不等于浏览器页面数量。缺少应用 owner 表结构时该指标显示暂不可用。资源模块通过受信任的 record_resource 接缝记录事件，不能使用插件自报的实例 ID 授权。

上线前须集中验证真实 PostgreSQL、浏览器失败链路、资源事件接入和适用客户端。测试夹具和网络替身结果不代表原生网络或全系统验收。

## 当前身份的资源观测

GET /v1/workbench/me/diagnostics/resource-metrics 只返回当前用户的应用资源状态及有效租约数量。不接收其他用户身份。registration 是真实数据库登记结果；runtime.complete=false 和运行数量 null 表示当前 provider 没有公开非消耗式枚举接口，不能推断没有进程。

集中测试可使用隔离 tests.workbench_observed_runner_fixture 与 tests.workbench_resource_observer.install(app)。两者要求显式 WB19_METRICS_TESTONLY=1 和隔离目录。后者添加 /workbench-test/me/resource-metrics，复用当前用户认证和 runner 控制令牌。仅 ScriptRunner 回环测试服务受支持。

测试观测读取实际进程句柄 returncode，不读取日志、路径或业务输出。runtime.appOwnerRunningWorkers 与 appOwnerStartingWorkers 分别表示真实运行句柄和正在创建的 owner 记录。只有 complete=true 且 provider=script_runner 时才可用于对应测试。未打开应用检查须同时断言 running=0 和 starting=0。这些测试专用端点不注册到生产或桌面入口。

隔离路径必须是当前源码工作树 .scratch/workbench-* 内的绝对目录。检查实际解析路径，拒绝父目录跳转、符号链接和业务 storage。两个适配器都强制 flag 与控制令牌存在。建议通过 python -m tests.workbench_observed_runner_fixture --port 5219 启动，入口固定绑定 127.0.0.1，并拒绝非回环的实际连接。
