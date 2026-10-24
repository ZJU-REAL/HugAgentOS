"""SQLAlchemy ORM models — admin console."""
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now


from core.db.engine import Base
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    String,
    Text,
)
from core.db.models.column_types import JSONType


class AdminSkill(Base):
    """Admin-managed skills stored in DB (replaces filesystem storage)."""

    __tablename__ = "admin_skills"

    skill_id = Column(String(100), primary_key=True)
    skill_content = Column(Text, nullable=False)  # full SKILL.md source text
    display_name = Column(String(255), nullable=False)  # denormalized field, avoids re-parsing
    description = Column(Text, nullable=False)
    user_intro = Column(
        Text, nullable=True
    )  # user-facing intro shown in the capability center (Markdown)
    version = Column(String(50), nullable=False, default="1.0.0")
    tags = Column(JSONType, default=list)
    allowed_tools = Column(JSONType, default=list)
    extra_files = Column(JSONType, default=dict)  # {filename: content}
    dependencies = Column(
        JSONType, default=dict
    )  # {"pip":[...], "npm":[...], "apt":[...], "warnings":[...]}
    is_enabled = Column(Boolean, nullable=False, default=True)
    # Dependency-readiness status: 'ready' = usable; 'installing' = missing packages detected,
    # admin notified, waiting for a sandbox rebuild to install them (soft-disabled: excluded from
    # runtime loading, but still shown in the skill list with a status label). Automatically
    # returns to 'ready' after the admin's sandbox rebuild succeeds.
    dep_status = Column(String(20), nullable=False, default="ready")
    # NULL = global skill (created by admin, visible to all users); non-null = a private skill
    # self-uploaded by a user, visible and usable only to that user.
    owner_user_id = Column(String(64), nullable=True)
    # Non-null = this skill was installed/imported by a plugin (plugin slug); on plugin uninstall
    # it is deleted precisely by this, without harming user-created skills.
    # See internal design docs.
    source_plugin = Column(String(100), nullable=True)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    created_by = Column(String(64))

    __table_args__ = (
        Index("idx_admin_skills_is_enabled", "is_enabled"),
        Index("idx_admin_skills_updated_at", "updated_at"),
        Index("idx_admin_skills_owner_user_id", "owner_user_id"),
        Index("idx_admin_skills_source_plugin", "source_plugin"),
    )


class SkillDependencyRequest(Base):
    """A "skill missing dependencies, pending admin installation" record.

    If an externally imported user skill is **verified in the sandbox** to
    actually have missing packages, a pending request is created and the
    skill is marked ``dep_status='installing'`` (soft-disabled). The admin
    has two paths: rebuild the sandbox (re-probe after the rebuild; only
    dependencies that are truly installed get set to ``satisfied`` and the
    skill restored to ``ready``); or reject (``rejected`` + reason, the
    skill stays soft-disabled with ``dep_status='rejected'``, and the reason
    is surfaced to the user).
    """

    __tablename__ = "skill_dependency_requests"

    request_id = Column(String(64), primary_key=True)
    skill_id = Column(String(100), nullable=False, index=True)
    user_id = Column(String(64), index=True)  # user who triggered the import (maps to a person)
    missing = Column(JSONType, default=dict)  # {"pip":[...], "npm":[...], "apt":[...]}
    status = Column(String(16), nullable=False, default="pending")  # pending | satisfied | rejected
    created_at = Column(UTCDateTime(timezone=True), default=utc_now, nullable=False)
    satisfied_at = Column(UTCDateTime(timezone=True))
    satisfied_by_run_id = Column(String(64))  # the sandbox_rebuild run_id that fulfilled it
    reason = Column(Text)  # rejection reason (optional), surfaced to the user
    rejected_at = Column(UTCDateTime(timezone=True))
    rejected_by = Column(String(64))  # identifier of the rejecting admin

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'satisfied', 'rejected')",
            name="skill_dependency_requests_status_check",
        ),
        Index("idx_skill_dep_requests_status", "status"),
    )


class AdminPromptPart(Base):
    """Admin-managed prompt parts stored in DB (overrides filesystem prompts)."""

    __tablename__ = "admin_prompt_parts"

    part_id = Column(String(100), primary_key=True)  # e.g. "system/00_role"
    content = Column(Text, nullable=False)
    display_name = Column(String(255), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    created_by = Column(String(64))

    __table_args__ = (
        Index("idx_admin_prompt_parts_sort_order", "sort_order"),
        Index("idx_admin_prompt_parts_is_enabled", "is_enabled"),
    )


class AdminMcpServer(Base):
    """Admin-managed MCP server configurations stored in DB."""

    __tablename__ = "admin_mcp_servers"

    server_id = Column(String(100), primary_key=True)
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False, default="")
    user_intro = Column(
        Text, nullable=True
    )  # user-facing intro shown in the capability center (Markdown)
    transport = Column(String(20), nullable=False, default="stdio")
    command = Column(String(500))
    args = Column(JSONType, default=list)
    url = Column(Text)
    env_vars = Column(JSONType, default=dict)
    env_inherit = Column(JSONType, default=list)
    headers = Column(JSONType, default=dict)
    is_enabled = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    extra_config = Column(JSONType, default=dict)
    tools_json = Column(JSONType, default=list)  # cached tool list from discovery
    icon = Column(String(500))  # optional icon URL (library path or uploaded asset)
    # NULL = global MCP (created by admin, visible to all users); non-null = a private remote MCP
    # self-added by a user, visible and usable only to that user.
    owner_user_id = Column(String(64), nullable=True)
    # Non-null = this MCP was installed/imported by a plugin (plugin slug); deleted precisely by this on plugin uninstall.
    source_plugin = Column(String(100), nullable=True)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    created_by = Column(String(64))

    __table_args__ = (
        CheckConstraint(
            "transport IN ('stdio', 'streamable_http', 'sse')",
            name="admin_mcp_servers_transport_check",
        ),
        Index("idx_admin_mcp_servers_is_enabled", "is_enabled"),
        Index("idx_admin_mcp_servers_sort_order", "sort_order"),
        Index("idx_admin_mcp_servers_owner_user_id", "owner_user_id"),
        Index("idx_admin_mcp_servers_source_plugin", "source_plugin"),
    )


class InstalledPlugin(Base):
    """An installed plugin (bundle) — the single source of truth for uninstall/upgrade.

    A plugin = an installable/removable unit bundling "skills + MCP (+ prompt
    fragments)"; on install the components are unpacked into ``AdminSkill`` /
    ``AdminMcpServer`` (each row tagged with ``source_plugin`` = this slug),
    while this table only records one "install record" + the list of
    component ids actually persisted, by which uninstall deletes precisely.

    Three sources are supported: in-repo plugin marketplace packages
    (``extensions/plugins/{default,marketplace}/<slug>/``), imported Claude Code
    plugins, and imported Codex plugins. See
    ``internal design docs``.
    """

    __tablename__ = "installed_plugins"

    # f"{slug}@{owner_user_id or 'global'}" — the same plugin can be installed privately by multiple users
    install_id = Column(String(160), primary_key=True)
    slug = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)  # display name
    version = Column(String(50), nullable=False, default="1.0.0")
    description = Column(Text, default="")
    category = Column(String(64), default="")
    # Library path / URL / inline data-URI (uploaded custom icons, capped at ~200KB by the service layer)
    icon = Column(Text)
    # NULL = global plugin (installed by admin, visible to all users); non-null = a user's private install.
    owner_user_id = Column(String(64), nullable=True)
    # builtin (built-in package) / imported_claude (imported CC plugin) / imported_codex (imported Codex plugin)
    source = Column(String(24), nullable=False, default="builtin")
    # Component ids actually persisted: {"skills":[...], "mcp":[...], "prompts":[...]}
    component_ids = Column(JSONType, default=dict)
    # Import report (imported / adapted / dropped), for front-end display
    import_report = Column(JSONType, default=dict)
    # UI contributions declared by the manifest (validated shape from
    # ``plugin_ui_contract``): tool views, canvas tabs, homepage shortcuts,
    # proxied data sources and self-shipped L2 modules. NULL = the plugin
    # contributes no interface. Lives on the install record so uninstalling or
    # disabling the plugin withdraws its UI in the same motion.
    ui_contributions = Column(JSONType)
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    created_by = Column(String(64))

    __table_args__ = (
        CheckConstraint(
            "source IN ('builtin', 'imported_claude', 'imported_codex')",
            name="installed_plugins_source_check",
        ),
        Index("idx_installed_plugins_owner", "owner_user_id"),
        Index("idx_installed_plugins_slug", "slug"),
    )
