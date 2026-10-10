"""Validate agent and MCP marketplace package contracts."""

import json

from pydantic import BaseModel, ConfigDict, Field

from .marketplace_version_archive import check_url, fail
from .marketplace_version_store import decode


class AgentPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    version: str = "1.0.0"
    name: str = Field(min_length=1, max_length=255)
    avatar: str = ""
    description: str = ""
    summary: str = ""
    system_prompt: str = Field(min_length=1)
    welcome_message: str = ""
    suggested_questions: list[str] = Field(default_factory=list, max_length=20)
    model_config_data: dict = Field(default_factory=dict, alias="model_config")
    bindings: dict[str, list[str]] = Field(default_factory=dict)
    ontology_tags: list[str] = Field(default_factory=list)


def validate(db, kind, config, old):
    if kind == "agent":
        try:
            config = AgentPackage.model_validate(config).model_dump(by_alias=True)
        except ValueError:
            fail("智能体配置格式无效")
        allowed = {"skill_ids", "mcp_server_ids", "plugin_ids", "kb_ids"}
        if set(config["bindings"]) - allowed:
            fail("智能体绑定字段无效")
        # Changing bindings is handled by the existing market publisher/reviewer.
        # Preserve reviewed bindings rather than silently adding unchecked access.
        previous = json.loads(decode(old)["agent.json"])
        if config["bindings"] != previous["bindings"]:
            fail("请通过智能体市场编辑入口调整能力绑定")
        from core.ontology.build_validator import ensure_ontology_build_valid

        ensure_ontology_build_valid(
            db,
            asset_type="subagent",
            name=config["name"],
            description=config["description"],
            instructions=config["system_prompt"],
            ontology_tags=config["ontology_tags"],
            **{k: v for k, v in config["bindings"].items() if k != "kb_ids"},
        )
    else:
        previous = json.loads(decode(old)["connector.json"])
        if set(config) != set(previous):
            fail("连接器配置字段无效")
        # Endpoint/auth/tool schemas are reviewed by the native MCP revalidation flow.
        # ZIP versions cannot bypass that review or change administrator credential origin.
        for key in (
            "transport",
            "url",
            "auth_schema",
            "auth_config",
            "tools_json",
            "tool_hash",
            "source_server_id",
        ):
            if config[key] != previous[key]:
                fail("连接或工具定义变化请先通过市场重新验证")
        check_url(config["url"])
        if not isinstance(config.get("listing_notice"), dict):
            fail("连接器说明格式无效")
    return config
