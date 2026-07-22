# 错误码参考

> 最后更新：2026-07-22

本文以代码实际实装为准：业务异常类定义在 `src/backend/core/infra/exceptions.py`（商业版 License 相关两个在 `src/backend/edition_ee/licensing/features.py`），由全局异常处理器 `src/backend/api/middleware/error_handler.py` 统一转换为[统一响应信封](overview.md#统一响应信封)。本文只列**已实装**的错误码；各分类号段内未列出的码位为预留空间。

## 错误响应结构

任何 `AppException` 子类抛出后，`error_handler` 都会产出标准信封（HTTP 状态码取自异常定义）：

```json
{
  "code": 40001,
  "message": "Resource not found",
  "data": {
    "resource_type": "chat",
    "resource_id": "chat_abc123",
    "hint": "The chat may have been deleted"
  },
  "trace_id": "req_1a2b3c4d5e6f7a8b",
  "timestamp": 1781136000000
}
```

`data` 携带各异常特有的上下文字段（见下表「附加字段」列），排查时优先把 `trace_id` 交给运维检索日志。

> **例外**：FastAPI 依赖层直接抛的 `HTTPException`（如 `require_admin` 的 401、`get_current_user` 的部分 401）不经过 `error_handler`，返回 FastAPI 原生 `{"detail": ...}` 结构——其中 `get_current_user` 把信封三件套（`code`/`message`/`data`）嵌进了 `detail` 里。前端对两种形态都做了兼容。

未登录访问受保护接口时的实际响应（`core/auth/backend.py`，非信封形态）：

```json
{
  "detail": {
    "code": 30001,
    "message": "Authorization required",
    "data": { "login_url": "/login" }
  }
}
```

而 `require_admin` / `require_config` token 不匹配时更简单，仅 `{"detail": "Unauthorized"}`（HTTP 401）；对应 token 环境变量未配置时返回 `{"detail": "... not configured"}`（HTTP 503）。

## 错误码分类规则

5 位业务码按首位分段（与 HTTP 状态码独立，但基本对应）：

| 号段 | 含义 | 典型 HTTP |
|---|---|---|
| `1xxxx` | 成功 | 200 / 201 |
| `2xxxx` | 客户端请求错误（参数、文件） | 400 |
| `3xxxx` | 认证与权限错误 | 401 / 403 |
| `4xxxx` | 资源状态错误（不存在、冲突、限流、license） | 404 / 409 / 429 / 402 |
| `5xxxx` | 服务端与上游依赖错误 | 500 / 502 / 503 / 504 |

## 成功码

| code | HTTP | 含义 | 来源 |
|---|---|---|---|
| 10000 | 200 | Success | `responses.success_response()` 默认值 |
| 10001 | 201 | Created | `responses.created_response()` |

## 已实装错误码

### 2xxxx — 客户端请求错误

| code | HTTP | 异常类 | 含义 | 典型触发场景 | 附加字段（`data`） |
|---|---|---|---|---|---|
| 20001 | 400 | `BadRequestError` / `ValidationError` | 请求参数不合法 / 校验失败 | 缺失必填字段、字段格式错误（两个异常类共用同一码，后者多带 `errors` 列表） | `errors?` |
| 21001 | 400 | `FileTooLargeError` | 文件过大 | 上传超过后端大小上限（Nginx 层超限则直接返回 413 HTML，无业务码） | `max_size`、`actual_size`、`unit` |
| 21002 | 400 | `InvalidFileTypeError` | 文件类型不支持 | 上传了白名单之外的扩展名/MIME | `allowed_types`、`actual_type` |

### 3xxxx — 认证与权限

| code | HTTP | 异常类 | 含义 | 典型触发场景 | 附加字段 |
|---|---|---|---|---|---|
| 30001 | 401 | `AuthenticationError` | 需要认证 | 未带会话 Cookie / Bearer；`get_current_user` 的 401 `detail` 里会附 `login_url` 引导跳转登录 | `login_url?` |
| 30002 | 401 | `InvalidTokenError` | Token / API-Key 无效或过期 | Bearer 校验失败；携带 `sk-jx-` 前缀但无效的 API-Key | — |
| 30003 | 401 | `TokenExpiredError` | Token 已过期 | 会话/票据过期 | `expired_at`、`hint` |
| 31001 | 403 | `AccessDeniedError` | 无权访问 | 访问他人资源、角色不满足 | `reason?` |
| 31002 | 403 | `InsufficientPermissionsError` | 权限不足 | 缺少某项具体权限（如 Lab、API-Key 开通权限） | `required_permission` |
| 31003 | 403 | `ResourceOwnershipError` | 仅资源所有者可操作 | 删除/修改非本人的会话、文件、分享 | `resource_type`、`resource_id`、`reason` |

### 4xxxx — 资源状态

| code | HTTP | 异常类 | 含义 | 典型触发场景 | 附加字段 |
|---|---|---|---|---|---|
| 40001 | 404 | `ResourceNotFoundError` | 资源不存在 | chat_id / kb_id / artifact_id 查无此项或已删除 | `resource_type`、`resource_id`、`hint` |
| 40002 | 404 | `EndpointNotFoundError` | API 端点不存在 | 请求了未注册的路径 | `path` |
| 40201 | **402** | `FeatureNotLicensed` | 当前 license 未授权该能力位 | CE/license 过期部署访问 EE 路由（teams、audit、admin 管理台等），见下文 | `feature` |
| 40202 | **402** | `SeatLimitExceeded` | 席位不足 / license 失效无法新增用户 | 注册/邀请用户超过 license 席位数 | 视场景 |
| 41001 | 409 | `ResourceAlreadyExistsError` | 资源已存在 | 重名创建（团队、技能 ID 等） | `resource_type`、`identifier` |
| 41002 | 409 | `ConcurrentModificationError` | 并发修改冲突 | 乐观锁版本号不匹配 | `expected_version`、`actual_version`、`hint` |
| 42001 | 429 | `RateLimitExceededError` | 请求频率超限 | 触发限流中间件 | `limit`、`retry_after`、`reset_at` |

### 5xxxx — 服务端与上游

| code | HTTP | 异常类 | 含义 | 典型触发场景 | 附加字段 |
|---|---|---|---|---|---|
| 50001 | 500 | `InternalServerError` | 服务器内部错误 | 未归类的运行时异常 | `error_type?`、`hint` |
| 50002 | 500 | `DatabaseError` | 数据库错误 | DB 连接/事务失败 | `error_type`、`hint` |
| 51001 | 500 | `StorageError` | 对象存储操作失败 | local/S3/OSS 读写失败（`message` 形如 `Storage upload failed`） | `error`、`hint` |
| 52001 | 502 | `UserCenterError` | 用户中心错误 | `AUTH_MODE=remote` 下用户中心调用失败 | `error` |
| 52101 | 502 | `ModelAPIError` | 模型服务错误 | LLM 端点返回错误 | `model`、`provider`、`error`、`hint` |
| 52103 | 400 | `ModelAPIRateLimitedError` | 模型配额超限 | 模型侧限流/配额耗尽（注意：实装的 HTTP 状态码是 400 而非 429） | `model`、`hint` |
| 53001 | 504 | `RequestTimeoutError` | 请求超时 | 依赖服务响应超时 | `service`、`timeout` |
| 53003 | 504 | `ModelAPITimeoutError` | 模型响应超时 | LLM 长时间无响应 | `model`、`timeout`、`hint` |
| 54001 | 503 | `ServiceUnavailableError` | 服务不可用 | 依赖未就绪、功能未配置（如内部接口未配 token） | — |

## License 未授权（HTTP 402）

EE 路由按 `edition_ee/routes/registry.py` 注册表挂载 License 能力位守卫（`edition_ee/licensing/deps.py` → `requires_feature`）。未授权时抛 `FeatureNotLicensed`，由 `error_handler` 兑现为：

```json
{
  "code": 40201,
  "message": "该功能未在当前 license 中授权: multi_tenancy",
  "data": { "feature": "multi_tenancy" },
  "trace_id": "req_...",
  "timestamp": 1781136000000
}
```

设计要点（`edition_ee/licensing/features.py`）：

- `FeatureNotLicensed` 是 402 信封的**唯一来源**，路由/服务层不允许再手搓 `HTTPException(402)`。
- 选 402 而非 403：403 会被前端当作会话失效触发强制登出，而 license 缺失不应把用户登出。
- `config_verify` / `config_license` / `auth` 三个 EE 路由显式豁免守卫——license 失效时仍需可达，否则无法更换 license。

详见 [License 与商业版](../editions/license.md)。

## 前端错误处理（`src/frontend/src/api.ts`）

统一请求函数 `apiRequest()` 对非 2xx 响应的处理顺序：

1. **401 / 403** → 调用经 `onUnauthorized()` 注册的全局回调，从 `payload.data.login_url` 或 `payload.detail.data.login_url` 取登录地址跳转，并抛 `Error('Session expired')`。**这就是 402 不用 403 的原因**——403 一律按会话失效登出。
2. **402** → 抛 `LicenseError`，由 UI 展示「功能未授权」提示而不登出。
3. **其他** → 抛通用 `Error`，消息优先取信封的 `message`。

上传场景有专门的友好化（`uploadErrorMessage()`）：HTTP 413（Nginx `client_max_body_size` 超限，HTML 响应无业务码）映射为「文件过大」提示；业务码 `21001` / `21002` 分别按 `data.max_size`、`data.allowed_types` 生成中文提示。

SSE 流中的错误不走 HTTP 状态码，而是以 `{"type": "error", "error": "..."}` 事件下发后接 `data: [DONE]`，见 [API 总览 · SSE 流式协议](overview.md#sse-流式协议)。

## 排查建议

1. **先看 `code` 再看 HTTP 状态**：业务码比 HTTP 状态码更细（如同为 401，`30001` 是没带凭证、`30002` 是凭证无效、`30003` 是已过期，处理方式不同）。
2. **拿 `trace_id` 查日志**：每个响应都带 `trace_id`，后端结构化日志（`core/infra/logging.py`）全程透传，EE 部署可在 `/v1/admin/logs/trace/{trace_id}` 直接检索整条链路。
3. **402 不是认证问题**：收到 `40201`/`40202` 说明部署的 license 不含该能力位，换 license 走 `POST /v1/config/license`，与用户凭证无关。
4. **413 无业务码是正常的**：超过 Nginx 上传上限时请求根本没到后端，返回的是 Nginx 的 HTML 错误页。
