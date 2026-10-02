"""Plugin system tests: import Claude Code / Codex / native plugin packages → persist to DB → uninstall.

Covers the three-tier portability matrix: direct skill import (including
references + path-variable rewriting), direct remote MCP import, stdio MCP
disabled on install, and drop warnings for hooks/commands/agents.
See internal design docs §10.
"""

import json


import pytest

from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin, UserShadow


from core.plugins.packaging import importer as pi

from core.plugins import management as ps

from core.plugins.management import components as plugin_components

from core.services.ontology_policy import set_plugin_import_build_validation_forced

OWNER = "test_user_123"


from tests.plugin_test_support import _make_cc_plugin, _zip_cc_plugin


def test_normalize_cc_plugin(tmp_path):
    pdir = _make_cc_plugin(tmp_path)
    np = pi.normalize_plugin_dir(pdir)

    assert np.kind == "claude"
    assert np.slug == "hello-toolkit"
    assert np.version == "2.1.0"

    # Skills: both are discovered
    names = sorted(s.name for s in np.skills)
    assert names == ["farewell", "hello-greeter"]
    greeter = next(s for s in np.skills if s.name == "hello-greeter")
    # references + scripts are carried over losslessly with the skill directory
    assert "references/style.md" in greeter.extra_files
    assert "scripts/greet.py" in greeter.extra_files

    # MCP: transport inferred correctly
    mcp = {m.name: m for m in np.mcp}
    assert mcp["weather-remote"].transport == "streamable_http"
    assert mcp["weather-remote"].needs_runtime is False
    assert mcp["local-fs"].transport == "stdio"
    assert mcp["local-fs"].needs_runtime is True
    # stdio path variables were rewritten
    assert not any("${CLAUDE_PLUGIN_ROOT}" in a for a in mcp["local-fs"].args)

    # required_secrets normalized from userConfig
    keys = [s["key"] for s in np.required_secrets]
    assert "api_token" in keys

    # Tier3 drops: hooks / commands / subagents all go into dropped
    dtypes = {d["type"] for d in np.dropped}
    assert "hooks" in dtypes
    assert "command" in dtypes
    assert "subagent" in dtypes


def test_import_cc_plugin_into_db(tmp_path, db_session):
    pdir = _make_cc_plugin(tmp_path)
    result = ps.import_plugin(
        db_session,
        pdir,
        owner_user_id=OWNER,
        secrets={"api_token": "sk-test-123"},
    )
    assert result["kind"] == "claude"
    assert result["source"] if "source" in result else True  # source set on row
    report = result["import_report"]
    # Two skills + one remote MCP go into imported; stdio MCP goes into adapted
    imported_types = [x["type"] for x in report["imported"]]
    assert imported_types.count("skill") == 2
    assert imported_types.count("mcp") == 1
    assert len(report["adapted"]) == 1
    assert report["adapted"][0]["name"] == "local-fs"
    assert len(report["dropped"]) >= 3

    # AdminSkill persisted with source_plugin tagged
    skills = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").all()
    assert len(skills) == 2
    for s in skills:
        assert s.owner_user_id == OWNER
        assert s.is_enabled is True
    greeter = next(s for s in skills if "hello-greeter" in s.skill_id)
    # Path variables rewritten to sandbox paths
    assert "${CLAUDE_PLUGIN_ROOT}" not in greeter.skill_content
    assert f"/workspace/skills/{greeter.skill_id}" in greeter.skill_content
    # Path variables inside references were rewritten too
    assert "${CLAUDE_PLUGIN_ROOT}" not in greeter.extra_files["references/style.md"]
    # Credentials written into secrets.json
    assert "secrets.json" in greeter.extra_files
    assert "sk-test-123" in greeter.extra_files["secrets.json"]

    # MCP persisted: remote enabled, stdio disabled
    remote = (
        db_session.query(AdminMcpServer)
        .filter(
            AdminMcpServer.source_plugin == "hello-toolkit",
            AdminMcpServer.transport == "streamable_http",
        )
        .first()
    )
    assert remote is not None and remote.is_enabled is True
    stdio = (
        db_session.query(AdminMcpServer)
        .filter(
            AdminMcpServer.source_plugin == "hello-toolkit",
            AdminMcpServer.transport == "stdio",
        )
        .first()
    )
    assert stdio is not None and stdio.is_enabled is False  # needs a runtime, disabled by default

    # Install record
    row = (
        db_session.query(InstalledPlugin)
        .filter(InstalledPlugin.owner_user_id == OWNER, InstalledPlugin.slug == "hello-toolkit")
        .first()
    )
    assert row is not None
    assert row.source == "imported_claude"
    assert len(row.component_ids["skills"]) == 2


@pytest.mark.parametrize(
    ("user_enabled", "forced", "expected_calls"),
    [
        (False, False, 0),
        (True, False, 4),
        (False, True, 4),
    ],
)
def test_plugin_import_ontology_validation_follows_user_and_force_policy(
    tmp_path,
    db_session,
    monkeypatch,
    user_enabled,
    forced,
    expected_calls,
):
    """Personal opt-out skips checks unless the independent admin gate is on."""
    owner = f"ontology_import_{int(user_enabled)}_{int(forced)}"
    db_session.add(
        UserShadow(
            user_id=owner,
            username=owner,
            extra_data={
                "ontology_enabled": user_enabled,
                "can_use_ontology_validation": user_enabled,
            },
        )
    )
    db_session.commit()
    if forced:
        set_plugin_import_build_validation_forced(db_session, True, updated_by="test")

    calls = []
    monkeypatch.setattr(
        plugin_components,
        "ensure_ontology_build_valid",
        lambda *_args, **kwargs: calls.append(kwargs["asset_type"]),
    )

    ps.import_plugin(
        db_session,
        _make_cc_plugin(tmp_path),
        owner_user_id=owner,
        secrets={"api_token": "test"},
    )

    assert len(calls) == expected_calls
    if expected_calls:
        assert calls.count("skill") == 2
        assert calls.count("tool") == 2


def test_owned_skill_enters_runtime_set(tmp_path, db_session):
    """Imported private skill with is_enabled=True → selected by the owned merge of resolve_all_runtime_enabled."""
    from core.config.catalog_resolver import _owned_enabled_ids

    pdir = _make_cc_plugin(tmp_path)
    ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "x"})

    owned_skills, owned_mcps = _owned_enabled_ids(db_session, OWNER, {})
    # Both skills are in the owned-enabled set
    assert sum(1 for s in owned_skills if "hello-toolkit" in s) == 2
    # Remote MCP is in; stdio is not (disabled)
    assert any("weather-remote" in m for m in owned_mcps)
    assert not any("local-fs" in m for m in owned_mcps)


def test_uninstall_removes_everything(tmp_path, db_session):
    pdir = _make_cc_plugin(tmp_path)
    res = ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "x"})
    install_id = res["install_id"]

    ps.uninstall_plugin(db_session, install_id, owner_user_id=OWNER)

    assert (
        db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").count()
        == 0
    )
    assert (
        db_session.query(AdminMcpServer)
        .filter(AdminMcpServer.source_plugin == "hello-toolkit")
        .count()
        == 0
    )
    assert (
        db_session.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).count()
        == 0
    )


def test_installed_detail_lists_components(tmp_path, db_session):
    """Installed detail returns skills (with instructions/files) + MCP (with transport/tools)."""
    pdir = _make_cc_plugin(tmp_path)
    res = ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "x"})
    detail = ps.get_installed_detail(db_session, res["install_id"], owner_user_id=OWNER)

    assert detail["name"]
    assert len(detail["skills"]) == 2
    greeter = next(s for s in detail["skills"] if "hello-greeter" in s["skill_id"])
    assert greeter["instructions"]  # body instructions (frontmatter stripped)
    assert "references/style.md" in greeter["files"]
    assert greeter["has_secrets"] is True  # credentials were injected
    # MCP components include transport
    transports = {m["transport"] for m in detail["mcp"]}
    assert "streamable_http" in transports and "stdio" in transports
    stdio = next(m for m in detail["mcp"] if m["transport"] == "stdio")
    assert stdio["needs_runtime"] is True

    # Unauthorized viewing must be rejected
    with pytest.raises(Exception):
        ps.get_installed_detail(db_session, res["install_id"], owner_user_id="someone_else")


def test_enable_disable_toggle(tmp_path, db_session):
    pdir = _make_cc_plugin(tmp_path)
    res = ps.import_plugin(db_session, pdir, owner_user_id=OWNER, secrets={"api_token": "x"})
    install_id = res["install_id"]

    ps.set_plugin_enabled(db_session, install_id, enabled=False, owner_user_id=OWNER)
    skills = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").all()
    assert all(s.is_enabled is False for s in skills)

    ps.set_plugin_enabled(db_session, install_id, enabled=True, owner_user_id=OWNER)
    skills = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "hello-toolkit").all()
    assert all(s.is_enabled is True for s in skills)
    # stdio MCP stays disabled even when the plugin is enabled as a whole
    stdio = (
        db_session.query(AdminMcpServer)
        .filter(
            AdminMcpServer.source_plugin == "hello-toolkit", AdminMcpServer.transport == "stdio"
        )
        .first()
    )
    assert stdio.is_enabled is False


def test_detect_codex_plugin(tmp_path):
    pdir = tmp_path / "codex-plug"
    (pdir / ".codex-plugin").mkdir(parents=True)
    (pdir / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "codex-plug",
                "version": "1.0.0",
                "description": "codex demo",
                "interface": {"composerIcon": "./assets/icon.png"},
            }
        ),
        encoding="utf-8",
    )
    sk = pdir / "skills" / "summarize"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: summarize\ndescription: Summarizes long text into bullet points\n---\nSummarize.\n",
        encoding="utf-8",
    )
    np = pi.normalize_plugin_dir(pdir)
    assert np.kind == "codex"
    assert np.icon == "./assets/icon.png"
    assert [s.name for s in np.skills] == ["summarize"]


def test_reject_non_plugin_dir(tmp_path):
    (tmp_path / "random.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(Exception):
        pi.normalize_plugin_dir(tmp_path)


def test_builtin_plugin_list_and_install(db_session):
    """The built-in sample plugin sample-translator should be discovered by list and be installable."""
    items = ps.list_plugins(db_session, owner_user_id=OWNER)
    slugs = {it["slug"] for it in items}
    assert "sample-translator" in slugs
    sample = next(it for it in items if it["slug"] == "sample-translator")
    assert sample["installed"] is False
    assert sample["skills_count"] == 1

    res = ps.install_plugin(db_session, "sample-translator", owner_user_id=OWNER)
    assert res["action"] == "installed"
    sk = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "sample-translator").all()
    assert len(sk) == 1
    assert sk[0].is_enabled is True
    # references are carried along with the skill
    assert any("glossary" in f for f in (sk[0].extra_files or {}))

    # List again → marked as installed
    items2 = ps.list_plugins(db_session, owner_user_id=OWNER)
    assert next(it for it in items2 if it["slug"] == "sample-translator")["installed"] is True


def test_builtin_plugin_component_ids_covers_static_mcp():
    """MCP/skill component ids declared in built-in plugin manifests should be collected by builtin_plugin_component_ids.

    Regression: MCPs of built-in plugins like automation_task / skill_manager
    bubble up statically via _ports.py → catalog.json as first-class entries,
    with no source_plugin row in the DB; they must be removed from the "MCP
    tool library" via filesystem manifest scanning and shown only under
    "Plugins".
    """
    skill_ids, mcp_ids = ps.builtin_plugin_component_ids()
    assert "automation_task" in mcp_ids
    assert "skill_manager" in mcp_ids
    # Plugin-bundled skills also go into the set (used to remove them from the skill library)
    assert "scheduled-tasks" in skill_ids
    assert "skill-creator" in skill_ids


def test_plugin_component_dedup_hides_static_plugin_mcp(db_session):
    """catalog._plugin_component_ids should include built-in plugins' static MCPs in the dedup set (union of DB + filesystem)."""
    from api.routes.v1.catalog import _plugin_component_ids

    _skill_ids, mcp_ids = _plugin_component_ids(db_session)
    assert "automation_task" in mcp_ids
    assert "skill_manager" in mcp_ids


def test_builtin_firecrawl_plugin_install(db_session):
    """Built-in firecrawl plugin (official CLI skill suite): discovered by list →
    installs 10 skills, all enabled, no MCP, no required_secrets (credentials
    are injected via system-config env) → clean uninstall."""
    items = ps.list_plugins(db_session, owner_user_id=OWNER)
    fc = next((it for it in items if it["slug"] == "firecrawl"), None)
    assert fc is not None, "firecrawl 插件未被 list_plugins 发现"
    assert fc["installed"] is False
    assert fc["skills_count"] == 10
    # Credentials are injected via system config, not collected in the plugin manifest
    assert not fc.get("required_secrets")

    res = ps.install_plugin(db_session, "firecrawl", owner_user_id=OWNER)
    assert res["action"] == "installed"

    sk = db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "firecrawl").all()
    assert len(sk) == 10
    assert all(s.is_enabled for s in sk)
    ids = {s.skill_id for s in sk}
    # Namespaced id = slug-skillname (per-user installs append a 6-char fingerprint); includes the main subcommands
    for expected in ("firecrawl-scrape", "firecrawl-search", "firecrawl-crawl", "firecrawl-cli"):
        assert any(i.startswith(expected) for i in ids), f"缺少技能 {expected}"
    # What gets installed is skills, not MCP (the official route uses the Bash(firecrawl *) CLI, not MCP)
    assert (
        db_session.query(AdminMcpServer).filter(AdminMcpServer.source_plugin == "firecrawl").count()
        == 0
    )
    # The umbrella skill is adapted to the platform: keeps the admin-config guidance, strips broken links to firecrawl-build/workflows
    cli = next(s for s in sk if s.skill_id.startswith("firecrawl-cli"))
    assert "plugin list (插件)" in cli.skill_content
    assert "firecrawl-build" not in cli.skill_content
    assert "firecrawl-workflows" not in cli.skill_content

    # Uninstall: all skills deleted, install record gone
    install_id = res["install_id"]
    out = ps.uninstall_plugin(db_session, install_id, owner_user_id=OWNER)
    assert out["removed_skills"] == 10
    assert db_session.query(AdminSkill).filter(AdminSkill.source_plugin == "firecrawl").count() == 0


def test_firecrawl_admin_config_schema_and_guards(db_session):
    """firecrawl's admin-level config (admin_config):
    - Market detail carries admin_config; the user view is read-only (no real-value field), configured computed with mode=any;
    - Admin view includes value (secrets masked);
    - Writes only accept field keys declared by the plugin (out-of-scope keys rejected)."""
    detail = ps.get_plugin_detail("firecrawl")
    ac = detail.get("admin_config")
    assert ac is not None, "firecrawl 市场详情缺 admin_config"
    assert ac["mode"] == "any"
    keys = {f["key"] for f in ac["fields"]}
    assert keys == {"firecrawl.api_key", "firecrawl.api_url"}
    # User view: never returns real values
    assert all("value" not in f for f in ac["fields"])
    assert ac["configured"] is False  # not configured in the test environment

    # Admin view: includes value, secrets masked
    admin_view = ps.get_plugin_admin_config("firecrawl")
    by_key = {f["key"]: f for f in admin_view["fields"]}
    assert "value" in by_key["firecrawl.api_key"] and by_key["firecrawl.api_key"]["secret"] is True
    assert by_key["firecrawl.api_url"]["secret"] is False

    # Writing an out-of-scope key is rejected
    with pytest.raises(Exception):
        ps.set_plugin_admin_config("firecrawl", {"dingtalk.client_id": "x"})


def test_import_from_zip_global(tmp_path, db_session):
    """Admin path: import_plugin_from_zip + owner=None → global install, visible via list_installed(None)."""
    zip_bytes = _zip_cc_plugin(tmp_path)
    res = ps.import_plugin_from_zip(
        db_session, zip_bytes, owner_user_id=None, secrets={"api_token": "g"}
    )
    assert res["kind"] == "claude"
    # Global skills/MCP (owner empty)
    sk = (
        db_session.query(AdminSkill)
        .filter(AdminSkill.source_plugin == "hello-toolkit", AdminSkill.owner_user_id.is_(None))
        .all()
    )
    assert len(sk) == 2
    glob = ps.list_installed(db_session, owner_user_id=None)
    assert any(p["slug"] == "hello-toolkit" for p in glob)
    # Not private to that user
    assert ps.list_installed(db_session, owner_user_id="someone") == []


def test_import_zip_normalizes_windows_backslash_paths(db_session):
    """ZIP entries written with Windows separators still form real skill directories on Linux."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "plugin.json",
            json.dumps(
                {
                    "name": "windows-path-plugin",
                    "version": "1.0.0",
                    "description": "Windows ZIP path compatibility",
                }
            ),
        )
        zf.writestr(
            "skills\\windows-path-skill\\SKILL.md",
            "---\nname: windows-path-skill\ndescription: Imported from a Windows ZIP\n---\n\nRun it.\n",
        )

    result = ps.import_plugin_from_zip(
        db_session,
        buf.getvalue(),
        owner_user_id=None,
    )

    assert result["slug"] == "windows-path-plugin"
    imported = result["import_report"]["imported"]
    assert [(item["type"], item["name"]) for item in imported] == [("skill", "windows-path-skill")]
