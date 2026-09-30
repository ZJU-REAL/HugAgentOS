"""Validated sandbox job callback payloads."""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentBody(BaseModel):
    # 字段名叫 schema_ 是为了避开 BaseModel 的保留名，对外仍是 "schema"
    model_config = ConfigDict(populate_by_name=True)

    prompt: str = ""
    schema_: Optional[Dict[str, Any]] = Field(default=None, alias="schema")
    tools: List[str] = []
    model: Optional[str] = None
    max_attempts: int = 2
    # 必须收 int：业务主键十有八九是行号/序号，脚本自然会写 item_key=it["seq"]。
    # Pydantic v2 不做 int→str 强转，只声明 str 会让整轮作业被 422 挡在门外——
    # 事故里 568 项全军覆没、模型一次都没被调用，就是这个类型洁癖的代价。
    item_key: Optional[Union[str, int]] = None

    @field_validator("item_key")
    @classmethod
    def _key_to_str(cls, v: Optional[Union[str, int]]) -> Optional[str]:
        return None if v is None or v == "" else str(v)


class LedgerBody(BaseModel):
    op: str
    items: Optional[List[Dict[str, Any]]] = None
    # 同 AgentBody.item_key：主键常常是整数序号，别让类型把回写挡在门外
    key: Optional[Union[str, int]] = None
    status: Optional[str] = None
    limit: Optional[int] = None
    result: Optional[Any] = None
    review: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    bump_attempts: bool = False


class LogBody(BaseModel):
    message: Optional[str] = None
    lifecycle: Optional[str] = None
    error: Optional[str] = None
