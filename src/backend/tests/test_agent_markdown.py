"""子智能体 markdown 导入导出格式测试（agent_markdown 服务）。"""

import io
import json
import zipfile
from decimal import Decimal

import pytest
from core.services.agent_markdown import (
    agent_to_markdown,
    agents_to_zip,
    parse_agent_markdown,
    parse_agents_upload,
)

SAMPLE = {
    "name": "数据分析师",
    "description": "擅长统计与可视化",
    "system_prompt": "你是一名数据分析师。\n\n## 职责\n- 清洗数据",
    "welcome_message": "你好，我可以帮你分析数据",
    "suggested_questions": ["帮我清洗CSV", "画个图"],
    "mcp_server_ids": ["database_query", "generate_chart_tool"],
    "skill_ids": ["data-analyst-cn"],
    "plugin_ids": ["chart-plugin"],
    "kb_ids": [],
    "model_provider_id": "provider-xxx",
    "temperature": Decimal("0.7"),
    "max_tokens": 4096,
    "max_iters": 10,
    "timeout": 120,
    "is_enabled": True,
    "sort_order": 0,
    "extra_config": {"ontology_tags": ["ontology:finance"]},
    "avatar": "📈",
}


def test_markdown_round_trip():
    text = agent_to_markdown(SAMPLE)
    assert text.startswith("---\n")
    item = parse_agent_markdown(text)
    assert item["name"] == SAMPLE["name"]
    assert item["system_prompt"] == SAMPLE["system_prompt"]
    assert item["mcp_server_ids"] == SAMPLE["mcp_server_ids"]
    assert item["skill_ids"] == SAMPLE["skill_ids"]
    assert item["plugin_ids"] == SAMPLE["plugin_ids"]
    assert item["model_provider_id"] == SAMPLE["model_provider_id"]
    assert item["temperature"] == pytest.approx(0.7)
    assert item["is_enabled"] is True
    assert item["extra_config"] == SAMPLE["extra_config"]
    assert item["avatar"] == SAMPLE["avatar"]
    assert "kb_ids" not in item  # 空列表导出时省略


def test_frontmatter_uses_harness_aliases():
    text = agent_to_markdown(SAMPLE)
    front = text.split("---")[1]
    assert "tools:" in front and "mcp_server_ids" not in front
    assert "skills:" in front and "skill_ids" not in front
    assert "model:" in front and "model_provider_id" not in front
    assert "enabled:" in front and "is_enabled" not in front


def test_parse_accepts_raw_field_names_and_comma_lists():
    text = "---\nname: 甲\nmcp_server_ids: [a, b]\nskills: x, y\n---\n提示词"
    item = parse_agent_markdown(text)
    assert item["mcp_server_ids"] == ["a", "b"]
    assert item["skill_ids"] == ["x", "y"]
    assert item["system_prompt"] == "提示词"


def test_parse_rejects_missing_frontmatter_or_name():
    with pytest.raises(ValueError):
        parse_agent_markdown("没有 frontmatter 的正文")
    with pytest.raises(ValueError):
        parse_agent_markdown("---\ndescription: 无名\n---\n正文")


def test_disabled_state_survives_round_trip():
    item = parse_agent_markdown(agent_to_markdown({**SAMPLE, "is_enabled": False}))
    assert item["is_enabled"] is False


def test_zip_round_trip_and_name_dedup():
    data = agents_to_zip([SAMPLE, {**SAMPLE, "name": "数据分析师/2"}])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
    assert len(names) == 2 and len(set(names)) == 2
    assert all(n.endswith(".md") for n in names)

    items = parse_agents_upload("agents.zip", data)
    assert {i["name"] for i in items} == {"数据分析师", "数据分析师/2"}


def test_upload_single_md_and_legacy_json():
    md = agent_to_markdown(SAMPLE).encode("utf-8")
    assert parse_agents_upload("agent.md", md)[0]["name"] == SAMPLE["name"]

    legacy = json.dumps([{"name": "旧格式", "system_prompt": "p"}]).encode("utf-8")
    assert parse_agents_upload("agents.json", legacy)[0]["name"] == "旧格式"

    with pytest.raises(ValueError):
        parse_agents_upload("agents.json", json.dumps({"name": "非数组"}).encode("utf-8"))
    with pytest.raises(ValueError):
        parse_agents_upload("agents.txt", b"whatever")
