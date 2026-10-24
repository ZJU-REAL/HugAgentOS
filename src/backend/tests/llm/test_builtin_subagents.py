from core.llm import prompt_files as prompt_files_module
from core.db.models import UserShadow
from core.capabilities.agents.builtin import BUILTIN_SUBAGENTS, build_builtin_runtime_profile, effective_builtin_capabilities, get_builtin_subagent, list_builtin_subagents, merge_builtin_subagents, refresh_builtin_subagents
from core.llm.subagents.tool import build_subagent_prompt_section
from core.llm import prompt_versions as prompt_version_service
from core.auth.users import UserService


def test_platform_defaults_are_exactly_explorer_worker_reviewer():
    assert [spec.agent_id for spec in BUILTIN_SUBAGENTS] == [
        "builtin.explorer",
        "builtin.worker",
        "builtin.reviewer",
    ]
    assert get_builtin_subagent("builtin.planner") is None


def test_context_policy_is_worker_inherits_others_independent():
    visible = list_builtin_subagents()
    policies = {item["agent_id"]: item["extra_config"]["context_policy"] for item in visible}

    assert policies == {
        "builtin.explorer": "independent_brief",
        "builtin.worker": "inherit_parent",
        "builtin.reviewer": "independent_brief",
    }


def test_every_role_inherits_but_never_invents_parent_capabilities():
    runtime = {
        "enabled_mcp_ids": ["internet_search", "automation_task"],
        "enabled_skill_ids": ["docs-writer"],
        "enabled_kb_ids": ["kb-team"],
    }

    for spec in BUILTIN_SUBAGENTS:
        assert effective_builtin_capabilities(spec, runtime) == {
            "mcp_server_ids": ["internet_search", "automation_task"],
            "skill_ids": ["docs-writer"],
            "kb_ids": ["kb-team"],
        }


def test_builtin_runtime_profile_follows_parent_grants():
    worker = get_builtin_subagent("builtin.worker")
    profile = build_builtin_runtime_profile(worker, {"enabled_mcp_ids": ["internet_search"]})
    assert profile.agent_id == "builtin.worker"
    assert profile.mcp_server_ids == ["internet_search"]
    assert "继承主智能体的完整对话上下文" in profile.system_prompt


def test_merge_prepends_defaults_and_blocks_reserved_id_shadowing():
    merged = merge_builtin_subagents(
        [
            {"agent_id": "custom.risk", "name": "风险分析"},
            {"agent_id": "builtin.worker", "name": "伪造执行员"},
        ]
    )

    assert [item["agent_id"] for item in merged] == [
        "builtin.explorer",
        "builtin.worker",
        "builtin.reviewer",
        "custom.risk",
    ]


def test_merge_omits_user_disabled_defaults_but_library_keeps_them_visible():
    runtime = merge_builtin_subagents(
        [{"agent_id": "builtin.worker", "name": "伪造执行员"}],
        disabled_agent_ids={"builtin.worker"},
    )
    assert [item["agent_id"] for item in runtime] == [
        "builtin.explorer",
        "builtin.reviewer",
    ]

    library = merge_builtin_subagents(
        [],
        disabled_agent_ids={"builtin.worker"},
        include_disabled=True,
        include_prompt=True,
    )
    by_id = {item["agent_id"]: item for item in library}
    assert by_id["builtin.explorer"]["is_enabled"] is True
    assert by_id["builtin.worker"]["is_enabled"] is False
    assert by_id["builtin.worker"]["owner_type"] == "builtin"
    assert by_id["builtin.worker"]["system_prompt"].strip()


def test_builtin_switch_is_persisted_per_user_and_defaults_to_enabled(db_session):
    db_session.add(UserShadow(user_id="builtin-pref-user", username="Builtin Pref", extra_data={}))
    db_session.commit()
    service = UserService(db_session)

    assert service.get_disabled_builtin_subagent_ids("builtin-pref-user") == set()

    service.set_builtin_subagent_enabled("builtin-pref-user", "builtin.reviewer", False)
    assert service.get_disabled_builtin_subagent_ids("builtin-pref-user") == {"builtin.reviewer"}

    service.set_builtin_subagent_enabled("builtin-pref-user", "builtin.reviewer", True)
    assert service.get_disabled_builtin_subagent_ids("builtin-pref-user") == set()


def test_refresh_uses_parent_final_grants_without_adding_or_inventing_tools():
    stale = list_builtin_subagents(
        {
            "enabled_mcp_ids": ["web_fetch", "site_publish"],
            "enabled_skill_ids": ["stale-skill"],
        }
    ) + [{"agent_id": "custom.risk", "name": "风险分析"}]
    refreshed = refresh_builtin_subagents(
        stale,
        {
            "enabled_mcp_ids": ["internet_search", "automation_task"],
            "enabled_skill_ids": ["docs-writer"],
            "enabled_kb_ids": ["kb-team"],
            "sandbox_tools_enabled": True,
            "code_capability_enabled": True,
        },
    )
    by_id = {item["agent_id"]: item for item in refreshed}

    assert by_id["builtin.explorer"]["mcp_server_ids"] == [
        "internet_search",
        "automation_task",
    ]
    assert by_id["builtin.explorer"]["skill_ids"] == ["docs-writer"]
    assert by_id["builtin.worker"]["mcp_server_ids"] == [
        "internet_search",
        "automation_task",
    ]
    assert by_id["builtin.worker"]["skill_ids"] == ["docs-writer"]
    assert by_id["custom.risk"] == {"agent_id": "custom.risk", "name": "风险分析"}


def test_main_prompt_explains_context_policy_to_router():
    section = build_subagent_prompt_section(
        list_builtin_subagents(
            {
                "enabled_mcp_ids": ["internet_search", "automation_task"],
                "enabled_skill_ids": ["docs-writer"],
                "enabled_kb_ids": ["kb-team"],
                "sandbox_tools_enabled": True,
                "code_capability_enabled": True,
            }
        )
    )

    assert "builtin.explorer" in section
    assert "继承主对话" in section
    assert "独立简报" in section
    assert "共享上下文=是" not in section
    assert "默认工具集" not in section
    assert "子智能体不能再往下委派" in section

    explorer_row = next(line for line in section.splitlines() if "builtin.explorer" in line)
    worker_row = next(line for line in section.splitlines() if "builtin.worker" in line)
    assert "MCP：internet_search, automation_task" in explorer_row
    assert "技能：docs-writer" in explorer_row
    assert "不写文件、不落盘" in explorer_row
    assert "需要写入或落盘的任务只交给它" in worker_row
    assert "MCP：internet_search, automation_task" in worker_row
    assert "技能：docs-writer" in worker_row
    assert "Bash" in worker_row


def test_main_prompt_omits_parent_disabled_capability_names():
    section = build_subagent_prompt_section(
        list_builtin_subagents(
            {
                "enabled_mcp_ids": ["internet_search"],
                "enabled_skill_ids": [],
                "enabled_kb_ids": [],
                "sandbox_tools_enabled": False,
                "code_capability_enabled": False,
            }
        )
    )

    assert "internet_search" in section
    assert "web_fetch" not in section
    assert "Bash；" not in section
    assert "Read/Edit/Write" not in section


def test_subagent_prompt_kind_has_three_independent_filesystem_parts():
    parts = prompt_files_module.read_fs_parts("subagents")

    assert [part["part_id"] for part in parts] == ["explorer", "reviewer", "worker"]
    assert all(part["content"].strip() for part in parts)
