# 外部应用认证

Config 管理员（商业版 EE）在「应用配置 → 外部应用认证」选择新应用或旧入口。认证与公开应用声明分开保存。写入携带当前 revision，冲突返回 409；读取不返回目标密钥或哈希。

- `independent`：打开经审核的目标地址，由目标自行登录。
- `shared_identity`：配置 `identityProvider` 和目标精确 `loginUrl`。双方分别接入同一身份提供方，目标执行已有 OIDC/SAML 登录，宿主不转发 Cookie 或通用令牌。
- `exchange`：配置 `audience`、精确 `callback`、`initiationUrl` 和至少 32 字符的随机目标服务端 `clientSecret`。票据默认 30 秒，`ttlSeconds` 允许 1–60 秒。发起和回调须与应用目标具有相同 HTTP(S) origin，回调不允许 fragment 或凭据查询参数。

接口位于 `/v1/workbench`，使用标准信封和 no-store 响应。代理须保留公开 Host 与协议。浏览器写请求须使用 JSON，Origin 须为宿主源或明确的 `CORS_ORIGINS` 白名单项；通配符不授予凭据写权限。

| 接口 | 调用方 / 结果 |
|---|---|
| GET/PUT /admin/external-auth/{key} | Config 管理员 / 脱敏配置和 revision |
| POST /applications/{key}/launch | 已认证用户 / 目标登录发起 URL |
| POST /external-auth/challenges | 已认证目标服务端 / 宿主继续登录 URL |
| GET /external-auth/continue | 浏览器 / 固定且有 CSP 的登录继续页 |
| POST /external-auth/authorize | 已认证宿主浏览器 / 目标回调 URL |
| POST /external-auth/exchange | 已认证目标服务端 / subject、audience、applicationKey |

## 目标适配器必需流程

目标必须实现完整浏览器登录事务。仅在宿主登记 state 不足以防登录 CSRF。

1. 启动返回 initiationUrl，fragment 中携带绑定发起宿主用户和会话的单次 intent。目标先清除 fragment，仅将 intent 交给自己的后端，再生成随机浏览器 state，保存有期限的单次目标登录事务。HTTPS 下设置 Secure、HttpOnly、SameSite=Lax 的 state/session Cookie。
2. 目标服务端用 `Authorization: Bearer <目标服务端密钥>` 调用 challenges，JSON 为 `{applicationKey,audience,callback,state,intent}`。密钥不进入浏览器或日志。
3. 将同一浏览器重定向到 data.continueUrl。challenge/state 通过 fragment 传递。宿主清除 fragment，认证自己的用户，并单次消费 challenge。
4. 宿主要求与发起用户一致；会话启动还须匹配原会话。账号或会话变化返回 launch_identity_mismatch。非 Cookie/原生入口须由浏览器认证同一用户，不能借用另一个浏览器账号；原生自动登录尚未验收。宿主重新检查当前权限和认证 revision，再将随机 ticket/state 放入精确回调的 fragment。票据不含明文身份，数据库只保存哈希。
5. 目标回调先清除 fragment，再加载其他资源。目标后端先将 state 与独立的预登录 Cookie 比较，并单次消费目标事务，再请求兑换。未发起、另一事务、重放和过期均拒绝；不能仅因为 state 随有效票据到达就接受。
6. 目标服务端用自己的 Bearer 密钥调用 exchange，JSON 为 `{applicationKey,audience,callback,ticket,state}`。成功后建立独立目标会话，失败须重新发起登录。

宿主 intent 和 challenge 的有效期各为 30 秒。目标事务须有上限，并覆盖 challenge 加票据时间。生产目标的多个 worker 须共享事务存储；受控测试适配器仅有一个隔离目标 worker。

SQLite/PostgreSQL 使用 DELETE RETURNING 原子消费 challenge 和票据。受众、精确回调、state、有效期或 revision 不匹配均拒绝。兑换时重新校验用户权限和应用状态。会话票据还要求签发时宿主会话有效；宿主退出使未兑换票据失效，但不退出已有第三方会话。


外部目标必须使用宿主会话 Cookie 作用域以外的 hostname。不同端口不能隔离 Cookie。启动与授权会拒绝宿主/API hostname、浏览器宿主 hostname，以及匹配会话 Domain 的域及子域，错误为 host_cookie_scope_overlap。检查涵盖发起、回调及共同身份提供方登录地址。不改变全局 Cookie 行为。测试使用不同 hostname、真实宿主会话和宿主专有 HttpOnly sentinel，验证目标均未收到。

## 旧配置迁移与恢复

通用 sso_token URL 拼接已移除。未配置旧入口显示 auth_configuration_required，保留名称和定义，不静默变成独立登录。管理员须逐项审核并显式保存认证方式。仍要求旧通用令牌的目标，须先部署受众限定适配器，再启用换票。已测试旧入口使用目标发起的 challenge 协议。不自动迁移真实配置。

配置使用既有 ContentBlock 的 workbench_external_auth: 键，目标密钥仅保存哈希。challenge/票据使用独立过期命名空间。认证更新在同一事务使未完成事务失效，并记录脱敏审计。不新增 schema。完整数据库快照保留配置；普通内容导出不包含配置。备份须受保护。恢复快照后轮换密钥或重新保存认证配置，使恢复的未完成登录事务失效。丢失明文密钥只能轮换。

降级前冻结写入并保留配置及 revision。不得恢复通用令牌转发；旧目标不能表达新协议时保持最后验证过的安全实现。

## 验证边界

测试覆盖公开 HTTP、SQLite/PostgreSQL 并发、不同浏览器源的受控目标、真实服务端兑换、Cookie、重放、未发起及不同浏览器登录 CSRF、旧入口显式适配、退出、脱敏和重启。身份均为隔离身份。共同身份提供方测试只验证目标登录入口，不代表真实企业 IdP 已登录。真实目标迁移、生产身份及 Windows/macOS/UOS 原生端须单独验收。
