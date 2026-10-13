"""Validated project environment configuration without transport dependencies."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FIELDS = ("agent_ids", "plugin_ids", "skill_ids", "mcp_ids")


class ProjectEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["all", "custom"] = "all"
    agent_ids: list[str] = Field(default_factory=list, max_length=500)
    plugin_ids: list[str] = Field(default_factory=list, max_length=500)
    skill_ids: list[str] = Field(default_factory=list, max_length=500)
    mcp_ids: list[str] = Field(default_factory=list, max_length=500)

    @field_validator(*FIELDS)
    @classmethod
    def validate_identifiers(cls, values: list[str]) -> list[str]:
        identifiers = [value.strip() for value in values]
        if any(not value or len(value) > 255 for value in identifiers):
            raise ValueError("资源标识不能为空或超过 255 字符")
        return list(dict.fromkeys(identifiers))

    @model_validator(mode="after")
    def clear_all_mode_bindings(self):
        if self.mode == "all":
            for field in FIELDS:
                setattr(self, field, [])
        return self
