"""SQLAlchemy ORM models — admin console."""

from datetime import datetime, timezone

from core.db.engine import Base
from core.db.model_extensions import MarketplaceListingEditionFields
from sqlalchemy import (
    JSON,
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")
INETType = String(45).with_variant(INET(), "postgresql")


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
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
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
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, nullable=False)
    satisfied_at = Column(TIMESTAMP(timezone=True))
    satisfied_by_run_id = Column(String(64))  # the sandbox_rebuild run_id that fulfilled it
    reason = Column(Text)  # rejection reason (optional), surfaced to the user
    rejected_at = Column(TIMESTAMP(timezone=True))
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
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
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
    is_stable = Column(Boolean, nullable=False, default=True)
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
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
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


class MarketplaceSubmission(Base):
    """A user's private skill "apply for listing on the skill marketplace" record (pending admin review).

    At submission time a content snapshot of the source ``AdminSkill`` is
    taken (skill_content / extra_files); review and listing are both based
    on the snapshot — the user later modifying or deleting the original
    skill does not affect listed content. Records with
    ``status='approved'`` appear in the skill marketplace list
    (source=community) and can be installed by everyone; the admin can
    reject at any time (including delisting already-listed items).
    """

    __tablename__ = "marketplace_submissions"

    submission_id = Column(String(64), primary_key=True)
    # Marketplace slug / entry_name after listing (derived at submission time,
    # globally unique, does not clash with the preset marketplace catalog)
    slug = Column(String(128), nullable=False, unique=True)
    skill_id = Column(String(100), nullable=False)  # source AdminSkill.skill_id
    owner_user_id = Column(String(64), nullable=False)  # applicant
    submitter_name = Column(
        String(255), default=""
    )  # applicant display name (denormalized, for display)

    display_name = Column(String(255), nullable=False)
    summary = Column(Text, default="")
    category = Column(String(64), default="社区共享")
    tags = Column(JSONType, default=list)
    version = Column(String(50), default="1.0.0")
    note = Column(Text, default="")  # application note (for the admin)

    skill_content = Column(Text, nullable=False)  # SKILL.md snapshot
    extra_files = Column(JSONType, default=dict)  # attachment snapshot {path: content}

    status = Column(String(16), nullable=False, default="pending")
    review_note = Column(Text)  # rejection reason / review note
    reviewed_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="marketplace_submissions_status_check",
        ),
        Index("idx_marketplace_submissions_status", "status", "created_at"),
        Index("idx_marketplace_submissions_owner", "owner_user_id"),
    )


class InstalledPlugin(Base):
    """An installed plugin (bundle) — the single source of truth for uninstall/upgrade.

    A plugin = an installable/removable unit bundling "skills + MCP (+ prompt
    fragments)"; on install the components are unpacked into ``AdminSkill`` /
    ``AdminMcpServer`` (each row tagged with ``source_plugin`` = this slug),
    while this table only records one "install record" + the list of
    component ids actually persisted, by which uninstall deletes precisely.

    Three sources are supported: in-repo plugin marketplace packages
    (``plugin_bundles/{default,marketplace}/<slug>/``), imported Claude Code
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
    icon = Column(String(500))
    # NULL = global plugin (installed by admin, visible to all users); non-null = a user's private install.
    owner_user_id = Column(String(64), nullable=True)
    # builtin (built-in package) / imported_claude (imported CC plugin) / imported_codex (imported Codex plugin)
    source = Column(String(24), nullable=False, default="builtin")
    # Component ids actually persisted: {"skills":[...], "mcp":[...], "prompts":[...]}
    component_ids = Column(JSONType, default=dict)
    # Import report (imported / adapted / dropped), for front-end display
    import_report = Column(JSONType, default=dict)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(String(64))

    __table_args__ = (
        CheckConstraint(
            "source IN ('builtin', 'imported_claude', 'imported_codex')",
            name="installed_plugins_source_check",
        ),
        Index("idx_installed_plugins_owner", "owner_user_id"),
        Index("idx_installed_plugins_slug", "slug"),
    )


class PluginMarketPackage(Base):
    """Admin-uploaded plugin marketplace package (stored/listed in the DB).

    Counterpart of the skill marketplace's ``MarketplaceSubmission``: a
    plugin zip uploaded by the admin from the /admin console is no longer
    installed globally right away, but persisted as a "market package" —
    storing the original zip's base64 (``package_b64``) + a set of display
    metadata (extracted by a single normalize pass). Once listed it appears
    in the plugin marketplace list for admins/users to install explicitly;
    installation unzips and re-runs the existing ``import_plugin`` chain.
    It coexists with filesystem preset packages
    (``plugin_bundles/{default,marketplace}/<slug>/``); when resolving by
    slug, **filesystem takes precedence, DB market package is the
    fallback**. Install records still go into ``InstalledPlugin``; this
    table is only an "installable source".
    """

    __tablename__ = "plugin_market_packages"

    slug = Column(String(100), primary_key=True)
    name = Column(String(255), nullable=False)
    version = Column(String(50), nullable=False, default="1.0.0")
    description = Column(Text, default="")
    category = Column(String(64), default="")
    icon = Column(String(500))
    # Package kind: native / claude / codex (determined by normalize), display only
    kind = Column(String(16), nullable=False, default="native")
    skills_count = Column(Integer, nullable=False, default=0)
    required_secrets = Column(JSONType, default=list)
    has_admin_config = Column(Boolean, nullable=False, default=False)
    package_b64 = Column(Text, nullable=False)  # base64 of the originally uploaded zip
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(String(64))

    __table_args__ = (Index("idx_plugin_market_packages_category", "category"),)


class PluginMarketSkillExclusion(Base):
    """A single skill "deleted" by the admin from a plugin in the plugin marketplace (excluded by plugin slug + skill name).

    The plugin marketplace originally only supported whole-package deletion;
    this table lets the admin precisely remove a specific skill within a
    plugin. The market list's ``skills_count``, market detail ``skills``,
    and DB persistence at install time are all filtered by it — built-in
    filesystem packages (whose source files cannot be modified at runtime)
    and DB-listed packages are uniformly covered by this exclusion list.
    Installed instances are unaffected (those are install snapshots, managed
    separately via enable/disable and uninstall).
    """

    __tablename__ = "plugin_market_skill_exclusions"

    slug = Column(String(100), primary_key=True)
    skill_name = Column(String(100), primary_key=True)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    created_by = Column(String(64))


class MarketplaceListingState(MarketplaceListingEditionFields, Base):
    """Marketplace listing switch: controls whether a plugin/skill is shown in the (plugin/skill) marketplace.

    ``kind`` = ``plugin`` | ``skill``; ``item_id`` = plugin slug or skill
    marketplace slug. **A missing row counts as enabled** (everything listed
    by default); a row with ``enabled=false`` is written only when the admin
    explicitly disables — so built-in/existing items are not wiped after an
    upgrade. The user-facing marketplace shows only enabled items; the admin
    console shows everything with toggles. Physical deletion (only for
    uploaded DB items) still goes through the respective delete endpoints;
    this table only governs "listing visibility".

    """

    __tablename__ = "marketplace_listing_states"

    kind = Column(String(16), primary_key=True)  # plugin | skill
    item_id = Column(String(160), primary_key=True)  # plugin slug / skill marketplace slug
    enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64))
