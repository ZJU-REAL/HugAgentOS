# API 响应信封参考

所有 `/v1/*` 端点返回统一的 JSON 信封格式。

## 信封结构

```json
{
  "code": 10000,
  "message": "Success",
  "data": { ... },
  "trace_id": "req_a1b2c3d4e5f6g7h8",
  "timestamp": 1710000000000
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | 业务状态码（见 error-codes.md） |
| `message` | string | 人可读消息 |
| `data` | any | 响应载荷（可以是 object、array、null） |
| `trace_id` | string | 请求追踪 ID，格式 `req_<hex16>` |
| `timestamp` | int | Unix 时间戳（毫秒） |

## 响应工具函数

定义在 `core/infra/responses.py`：

### `success_response(data, message, code, trace_id)`
```python
# 默认 code=10000
return success_response(data={"id": "123", "name": "foo"})
```

### `created_response(data, message, trace_id)`
```python
# code=10001, message="Resource created successfully"
return created_response(data={"id": "new_123"})
```

### `paginated_response(items, page, page_size, total_items, message, trace_id)`
```python
# data 包含 items + pagination
return paginated_response(
    items=[{"id": "1"}, {"id": "2"}],
    page=1,
    page_size=20,
    total_items=42,
)
```

返回结构：
```json
{
  "code": 10000,
  "data": {
    "items": [...],
    "pagination": {
      "page": 1,
      "page_size": 20,
      "total_items": 42,
      "total_pages": 3,
      "has_previous": false,
      "has_next": true
    }
  }
}
```

### `error_response(code, message, data, status_code, trace_id)`
```python
# 返回 JSONResponse（通常不直接使用，而是抛异常让全局 handler 处理）
return error_response(code=20001, message="Bad request", status_code=400)
```

## 规则

1. **路由中只用 `success_response` / `created_response` / `paginated_response`**
2. **错误通过抛 `AppException` 子类，由全局 handler 转 `error_response`**
3. **trace_id 默认自动生成，无需手动传**
4. **SSE 端点不使用信封**：用 `sse_response(generator, extra_headers=...)` 返回（自带 `Cache-Control: no-cache`、
   `X-Accel-Buffering: no` 等 `SSE_HEADERS`）；聊天回合的 SSE 输出统一走 `api/transports/run_sse.py`，不要自写一套
5. **License 402** 只来自 `edition_ee/licensing/features.py` 的 `FeatureNotLicensed`（40201）/ `SeatLimitExceeded`（40202），
   路由与服务层不要手搓 402
6. 时间字段序列化用 `.isoformat()`（模型存的是带时区的 UTC），不要输出无时区字符串
