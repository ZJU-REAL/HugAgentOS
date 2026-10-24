"""OpenAPI schema and documentation metadata."""
import os
from core.config.settings import settings

def _build_custom_openapi(app):
    from api.openapi_data_schemas import DATA_COMPONENTS, DATA_SCHEMAS
    from api.openapi_edition_schemas import EDITION_DATA_COMPONENTS, EDITION_DATA_SCHEMAS
    from fastapi.openapi.utils import get_openapi

    def _custom_openapi():
        if app.openapi_schema:
            # The heavy schema is cached, but the brand title is refreshed from the admin-platform config every time (renames take effect immediately)
            app.openapi_schema["info"]["title"] = _docs_title()
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        components = schema.setdefault("components", {}).setdefault("schemas", {})

        # Inject business-entity component schemas (from the central registry)
        for name, comp_schema in {**DATA_COMPONENTS, **EDITION_DATA_COMPONENTS}.items():
            if name not in components:
                components[name] = comp_schema

        if "ApiResponseEnvelope" not in components:
            components["ApiResponseEnvelope"] = {
                "title": "ApiResponseEnvelope",
                "type": "object",
                "description": (
                    "统一 API 响应包络。所有 /v1/* 接口都返回此结构；"
                    "实际业务数据放在 `data` 字段中（不同端点结构不同）。"
                ),
                "required": ["code", "message", "trace_id", "timestamp"],
                "properties": {
                    "code": {
                        "type": "integer",
                        "default": 10000,
                        "description": "业务状态码（10000 = 成功；非 10000 表示业务错误）",
                    },
                    "message": {
                        "type": "string",
                        "default": "Success",
                        "description": "人类可读消息（成功/失败描述）",
                    },
                    "data": {
                        "description": (
                            "端点特定的业务数据。不同接口结构不同——"
                            "通用结构是 object/array/null，详细字段见各接口的"
                            "「响应」描述或后端服务层定义。"
                        ),
                    },
                    "trace_id": {
                        "type": "string",
                        "example": "req_a1b2c3d4e5f60718",
                        "description": "请求追踪 ID（用于日志关联），格式 `req_<16hex>`",
                    },
                    "timestamp": {
                        "type": "integer",
                        "format": "int64",
                        "example": 1735660800000,
                        "description": "响应生成时间，Unix 毫秒时间戳",
                    },
                },
            }

        envelope_ref = {"$ref": "#/components/schemas/ApiResponseEnvelope"}

        def _is_empty_schema(s):
            if not isinstance(s, dict):
                return True
            if not s:
                return True
            informative_keys = {
                "$ref",
                "type",
                "properties",
                "items",
                "allOf",
                "anyOf",
                "oneOf",
                "enum",
                "additionalProperties",
                "format",
            }
            return not any(k in s for k in informative_keys)

        def _envelope_schema_for(method_upper: str, path: str) -> dict:
            """Registry hit → allOf merge; otherwise fall back to a plain envelope ref."""
            data_schema = EDITION_DATA_SCHEMAS.get(
                (method_upper, path), DATA_SCHEMAS.get((method_upper, path))
            )
            if data_schema is None:
                return dict(envelope_ref)
            return {
                "allOf": [
                    dict(envelope_ref),
                    {
                        "type": "object",
                        "properties": {"data": data_schema},
                    },
                ],
            }

        for path, methods in (schema.get("paths") or {}).items():
            if not path.startswith("/v1/"):
                continue
            if not isinstance(methods, dict):
                continue
            for method, op in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if not isinstance(op, dict):
                    continue
                method_upper = method.upper()
                responses = op.get("responses") or {}
                for status, resp in responses.items():
                    if not (isinstance(status, str) and status.startswith("2")):
                        continue
                    if not isinstance(resp, dict):
                        continue
                    content = resp.get("content")
                    if not isinstance(content, dict):
                        # When no content is declared, add one too so the frontend can render the envelope
                        resp["content"] = {
                            "application/json": {"schema": _envelope_schema_for(method_upper, path)}
                        }
                        continue
                    for media, payload in content.items():
                        if not isinstance(media, str) or not media.startswith("application/json"):
                            continue
                        if not isinstance(payload, dict):
                            continue
                        if _is_empty_schema(payload.get("schema")):
                            payload["schema"] = _envelope_schema_for(method_upper, path)

        schema["info"]["title"] = _docs_title()
        app.openapi_schema = schema
        return schema

    return _custom_openapi

def _docs_title() -> str:
    from api.app import app
    """Title for the docs pages (Swagger / ReDoc) and OpenAPI info.title — fetched
    live from the admin-platform config (product_name + API-docs label),
    removing the hard-coded brand. Falls back to app.title when the DB read
    fails (already the env brand, no literal "HugAgentOS")."""
    try:
        from core.files.content.branding import get_admin_platform_info

        info = get_admin_platform_info()
        name = (info.get("product_name") or "").strip()
        label = (info.get("apidoc_label") or "").strip()
        joined = " ".join(p for p in (name, label) if p)
        return joined or app.title
    except Exception:
        return app.title
