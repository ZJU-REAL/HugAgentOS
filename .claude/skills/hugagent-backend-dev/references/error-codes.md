# 错误码完整参考

所有错误码定义在 `core/infra/exceptions.py`，全局 handler 在 `api/middleware/error_handler.py`。

## 码段分配

| 范围 | HTTP | 类别 | 说明 |
|------|------|------|------|
| **10000** | 200 | 成功 | 通用成功 |
| **10001** | 201 | 成功 | 资源已创建 |

### 2xxxx — 客户端请求错误

| Code | HTTP | Exception Class | 说明 |
|------|------|----------------|------|
| 20001 | 400 | `BadRequestError` | 参数无效 |
| 20001 | 400 | `ValidationError` | 字段校验失败（含 errors 数组） |
| 21001 | 400 | `FileTooLargeError` | 文件超过大小限制 |
| 21002 | 400 | `InvalidFileTypeError` | 文件类型不允许 |

### 3xxxx — 认证/授权错误

| Code | HTTP | Exception Class | 说明 |
|------|------|----------------|------|
| 30001 | 401 | `AuthenticationError` | 需要认证 |
| 30002 | 401 | `InvalidTokenError` | Token 无效或过期 |
| 30003 | 401 | `TokenExpiredError` | Token 已过期（含 expired_at） |
| 31001 | 403 | `AccessDeniedError` | 拒绝访问 |
| 31002 | 403 | `InsufficientPermissionsError` | 权限不足 |
| 31003 | 403 | `ResourceOwnershipError` | 非资源所有者 |

### 4xxxx — 资源错误

| Code | HTTP | Exception Class | 说明 |
|------|------|----------------|------|
| 40001 | 404 | `ResourceNotFoundError` | 资源未找到 |
| 40002 | 404 | `EndpointNotFoundError` | API 端点不存在 |
| 41001 | 409 | `ResourceAlreadyExistsError` | 资源已存在 |
| 41002 | 409 | `ConcurrentModificationError` | 并发修改冲突 |
| 42001 | 429 | `RateLimitExceededError` | 速率限制 |

### 5xxxx — 服务端错误

| Code | HTTP | Exception Class | 说明 |
|------|------|----------------|------|
| 50001 | 500 | `InternalServerError` | 内部错误 |
| 50002 | 500 | `DatabaseError` | 数据库错误 |
| 51001 | 500 | `StorageError` | 存储操作失败 |
| 52001 | 502 | `UserCenterError` | 用户中心错误 |
| 52101 | 502 | `ModelAPIError` | 模型 API 错误 |
| 52103 | 400 | `ModelAPIRateLimitedError` | 模型配额超限 |
| 53001 | 504 | `RequestTimeoutError` | 请求超时 |
| 53003 | 504 | `ModelAPITimeoutError` | 模型 API 超时 |
| 54001 | 503 | `ServiceUnavailableError` | 服务不可用 |

## 新增错误码规则

1. 按类别选择码段（2xxxx/3xxxx/4xxxx/5xxxx）
2. 同类别内递增分配（如 21xxx 用于文件相关）
3. 每个码对应一个 Exception 子类
4. Exception 构造函数中设置 code、message、status_code、data
5. data 字段中包含调试信息（resource_type、hint 等）
