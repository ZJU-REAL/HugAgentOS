"""Limits and versioned-field lists for user-defined agents."""
from __future__ import annotations


MAX_USER_AGENTS = 20


DEFAULT_AGENT_VERSION = "V1.0"


MAX_CHANGE_HISTORY = 30


NON_VERSIONED_FIELDS = {"is_enabled"}


VERSIONED_FIELDS = {
    "name": "名称",
    "description": "简介",
    "system_prompt": "角色设定",
    "welcome_message": "开场白",
    "suggested_questions": "推荐问题",
    "mcp_server_ids": "绑定工具",
    "skill_ids": "绑定技能",
    "plugin_ids": "绑定插件",
    "kb_ids": "绑定知识库",
    "model_provider_id": "模型",
    "temperature": "温度",
    "max_tokens": "最大输出长度",
    "max_iters": "最大推理轮次",
    "timeout": "超时时间",
    "is_enabled": "启用状态",
}
