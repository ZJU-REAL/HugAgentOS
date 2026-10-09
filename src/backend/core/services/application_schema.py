"""Validated application database definitions; generated SQL is never accepted."""

from __future__ import annotations

import keyword
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_RESERVED = {"id", "version", "created_at", "_request_key"}
_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,47}$")


def identifier(value: str) -> str:
    if not _PATTERN.fullmatch(value) or value in _RESERVED:
        raise ValueError("Use a lowercase identifier; reserved column names are unavailable")
    return value


class ColumnDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: Literal["text", "integer", "number", "boolean", "date", "json"] = "text"
    required: bool = False
    unique: bool = False
    indexed: bool = False
    max_length: int = Field(2000, ge=1, le=10000)
    name_is_valid = field_validator("name")(identifier)

    @model_validator(mode="after")
    def json_constraints(self):
        if self.type == "json" and (self.unique or self.indexed):
            raise ValueError("JSON columns do not support unique or indexed flags")
        return self


class TableDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    columns: list[ColumnDefinition] = Field(min_length=1, max_length=40)
    public_insert: bool = False
    public_read: bool = False
    public_replace: bool = False
    name_is_valid = field_validator("name")(identifier)

    @model_validator(mode="after")
    def distinct_columns(self):
        if len({c.name for c in self.columns}) != len(self.columns):
            raise ValueError("Column names must be distinct")
        return self


class ApplicationDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    site_id: str = Field("", max_length=64)
    kind: Literal["data", "mcp"] = "data"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = Field(min_length=1, max_length=1000)
    table: str
    fields: list[str] = Field(min_length=1, max_length=40)
    filters: list[str] = Field(default_factory=list, max_length=40)
    name_is_valid = field_validator("name", "table")(identifier)

    @model_validator(mode="after")
    def distinct_fields(self):
        for names in (self.fields, self.filters):
            if len(names) != len(set(names)) or any(
                name in {"limit", "offset"} or name.startswith("model_") or keyword.iskeyword(name)
                for name in names
            ):
                raise ValueError("Duplicate or reserved MCP parameter names")
        return self


class RecordBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    request_key: str | None = Field(None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def bounded_payload(self):
        import json

        encoded = [
            len(json.dumps(row, ensure_ascii=False, allow_nan=False).encode()) for row in self.rows
        ]
        if max(encoded) > 16384 or sum(encoded) > 524288:
            raise ValueError("Rows are limited to 16 KiB; batches to 512 KiB")
        return self


ApplicationAction = Literal[
    "list",
    "create",
    "table",
    "insert",
    "query",
    "import_industry",
    "publish_mcp",
    "revoke_mcp",
    "source",
    "publish_project",
]


class MCPPublish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[ToolDefinition] = Field(min_length=1, max_length=30)


class MCPProjectDefinition(MCPPublish):
    app_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: int = Field(ge=0)


class MCPRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)


class PatchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    values: dict[str, Any]


class InsertOperation(RecordBatch):
    table: str
    valid_table = field_validator("table")(identifier)


class QueryOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table: str
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(50, ge=1, le=100)
    offset: int = Field(0, ge=0, le=100000)
    valid_table = field_validator("table")(identifier)


class InternalOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(min_length=1, max_length=64)
    chat_id: str = ""
    action: ApplicationAction
    app_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class IndustryImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword: str = Field(min_length=1, max_length=100)
    limit: int = Field(5, ge=1, le=50)


ApplicationPayload = (
    ApplicationDefinition | TableDefinition | InsertOperation | QueryOperation | MCPPublish
)


class InternalMCPPublish(MCPPublish):
    user_id: str = Field(min_length=1, max_length=64)
    chat_id: str = ""
    app_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class RestoreCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
