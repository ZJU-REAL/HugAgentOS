"""Marketplace submissions, listings, versions and installations."""
from core.db.utc_datetime import UTCDateTime
from core.infra.time import utc_now
from datetime import datetime, timezone
from core.db.engine import Base
from core.db.edition import MarketplaceListingEditionFields
from sqlalchemy import JSON, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import mapped_column, relationship
from core.db.edition import UserAgentEditionFields, user_agent_edition_table_args
from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
JSONType = JSON().with_variant(JSONB(), "postgresql")

class McpMarketItem(Base):
    """Installable MCP marketplace entry.

    The marketplace stores a credential-free definition.  Concrete credentials
    and enablement live on the ``AdminMcpServer`` created for each installation.
    ``latest_version_id`` points at an immutable ``McpMarketVersion`` snapshot.
    """

    __tablename__ = "mcp_market_items"

    slug = Column(String(128), primary_key=True)
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False, default="")
    user_intro = Column(Text)
    category = Column(String(64), nullable=False, default="通用工具")
    tags = Column(JSONType, default=list)
    icon = Column(String(500))
    publisher_id = Column(String(64))
    publisher_name = Column(String(255), nullable=False, default="")
    source = Column(String(16), nullable=False, default="community")
    latest_version_id = Column(String(64), nullable=False)
    # active = installable; changed = remote tool drift awaiting review;
    # suspended = security kill switch (derived installations are disabled).
    status = Column(String(16), nullable=False, default="active")
    status_reason = Column(Text)
    last_verified_at = Column(UTCDateTime(timezone=True))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    deleted_at = Column(UTCDateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "source IN ('admin', 'community')",
            name="mcp_market_items_source_check",
        ),
        CheckConstraint(
            "status IN ('active', 'changed', 'suspended')",
            name="mcp_market_items_status_check",
        ),
        Index("idx_mcp_market_items_status", "status", "updated_at"),
        Index("idx_mcp_market_items_category", "category"),
    )

class McpMarketVersion(Base):
    """Immutable, reviewed MCP marketplace version snapshot."""

    __tablename__ = "mcp_market_versions"

    version_id = Column(String(64), primary_key=True)
    slug = Column(
        String(128),
        ForeignKey("mcp_market_items.slug", ondelete="CASCADE"),
        nullable=False,
    )
    version = Column(String(50), nullable=False, default="1.0.0")
    transport = Column(String(20), nullable=False, default="streamable_http")
    url = Column(Text, nullable=False)
    # [{key, label, target: "header" | "query" | "url", required, secret, ...}];
    # values are never stored here.
    auth_schema = Column(JSONType, default=list)
    # Provider-neutral authentication contract.  ``methods`` may contain
    # none/token/oauth2 options; OAuth endpoint discovery follows MCP metadata.
    auth_config = Column(JSONType, default=dict)
    tools_json = Column(JSONType, default=list)
    tool_hash = Column(String(64), nullable=False)
    listing_notice = Column(JSONType, default=dict)
    source_server_id = Column(String(100))
    approved_by = Column(String(64))
    approved_at = Column(UTCDateTime(timezone=True))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "transport IN ('streamable_http', 'sse')",
            name="mcp_market_versions_transport_check",
        ),
        UniqueConstraint("slug", "version", name="uq_mcp_market_versions_slug_version"),
        Index("idx_mcp_market_versions_slug", "slug", "created_at"),
    )

class McpMarketSubmission(Base):
    """Credential-free snapshot submitted by a user for MCP marketplace review."""

    __tablename__ = "mcp_market_submissions"

    submission_id = Column(String(64), primary_key=True)
    slug = Column(String(128), nullable=False)
    source_server_id = Column(String(100), nullable=False)
    owner_user_id = Column(String(64), nullable=False)
    submitter_name = Column(String(255), nullable=False, default="")
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False, default="")
    user_intro = Column(Text)
    category = Column(String(64), nullable=False, default="通用工具")
    tags = Column(JSONType, default=list)
    icon = Column(String(500))
    version = Column(String(50), nullable=False, default="1.0.0")
    transport = Column(String(20), nullable=False)
    url = Column(Text, nullable=False)
    auth_schema = Column(JSONType, default=list)
    auth_config = Column(JSONType, default=dict)
    tools_json = Column(JSONType, default=list)
    tool_hash = Column(String(64), nullable=False)
    listing_notice = Column(JSONType, default=dict)
    note = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="pending")
    review_note = Column(Text)
    reviewed_by = Column(String(64))
    reviewed_at = Column(UTCDateTime(timezone=True))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
    deleted_at = Column(UTCDateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn')",
            name="mcp_market_submissions_status_check",
        ),
        CheckConstraint(
            "transport IN ('streamable_http', 'sse')",
            name="mcp_market_submissions_transport_check",
        ),
        Index("idx_mcp_market_submissions_status", "status", "created_at"),
        Index("idx_mcp_market_submissions_owner", "owner_user_id", "created_at"),
        Index("idx_mcp_market_submissions_slug", "slug"),
    )

class McpMarketInstallation(Base):
    """Links a reviewed market version to the concrete installed MCP server."""

    __tablename__ = "mcp_market_installations"

    install_id = Column(String(64), primary_key=True)
    slug = Column(
        String(128),
        ForeignKey("mcp_market_items.slug", ondelete="CASCADE"),
        nullable=False,
    )
    version_id = Column(
        String(64),
        ForeignKey("mcp_market_versions.version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    server_id = Column(
        String(100),
        ForeignKey("admin_mcp_servers.server_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # NULL means the admin installed it globally; otherwise it is private to the user.
    owner_user_id = Column(String(64))
    status = Column(String(16), nullable=False, default="active")
    installed_by = Column(String(64))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'suspended')",
            name="mcp_market_installations_status_check",
        ),
        Index("idx_mcp_market_installations_owner", "owner_user_id", "created_at"),
        Index("idx_mcp_market_installations_slug", "slug", "status"),
        Index(
            "uq_mcp_market_installations_user_slug",
            "slug",
            "owner_user_id",
            unique=True,
            postgresql_where=text("owner_user_id IS NOT NULL"),
            sqlite_where=text("owner_user_id IS NOT NULL"),
        ),
        Index(
            "uq_mcp_market_installations_global_slug",
            "slug",
            unique=True,
            postgresql_where=text("owner_user_id IS NULL"),
            sqlite_where=text("owner_user_id IS NULL"),
        ),
    )

class McpMarketOAuthClient(Base):
    """Administrator-registered OAuth App used by every installer of one MCP listing.

    Some authorization servers (GitHub, many enterprise IdPs) support neither
    dynamic registration nor client metadata documents, so an administrator
    registers one OAuth App and each user only grants consent.  The client
    secret is encrypted here and never copied into market items or versions;
    each user's access/refresh tokens still live on their own installation.
    """

    __tablename__ = "mcp_market_oauth_clients"

    slug = Column(
        String(128),
        ForeignKey("mcp_market_items.slug", ondelete="CASCADE"),
        primary_key=True,
    )
    client_id = Column(String(512), nullable=False)
    client_secret = Column(Text)
    updated_by = Column(String(64))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(
        UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
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
    user_intro = Column(Text)  # explicit user-facing intro snapshot; NULL falls back to SKILL.md
    category = Column(String(64), default="社区共享")
    tags = Column(JSONType, default=list)
    version = Column(String(50), default="1.0.0")
    note = Column(Text, default="")  # application note (for the admin)

    skill_content = Column(Text, nullable=False)  # SKILL.md snapshot
    extra_files = Column(JSONType, default=dict)  # attachment snapshot {path: content}

    status = Column(String(16), nullable=False, default="pending")
    review_note = Column(Text)  # rejection reason / review note
    reviewed_at = Column(UTCDateTime(timezone=True))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="marketplace_submissions_status_check",
        ),
        Index("idx_marketplace_submissions_status", "status", "created_at"),
        Index("idx_marketplace_submissions_owner", "owner_user_id"),
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
    (``extensions/plugins/{default,marketplace}/<slug>/``); when resolving by
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
    # Library path / URL / inline data-URI (uploaded custom icons, capped at ~200KB by the service layer)
    icon = Column(Text)
    # Package kind: native / claude / codex (determined by normalize), display only
    kind = Column(String(16), nullable=False, default="native")
    skills_count = Column(Integer, nullable=False, default=0)
    required_secrets = Column(JSONType, default=list)
    has_admin_config = Column(Boolean, nullable=False, default=False)
    package_b64 = Column(Text, nullable=False)  # base64 of the originally uploaded zip
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
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
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
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
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)
    updated_by = Column(String(64))

class AgentMarketSubmission(Base):
    """Record of a user-created sub-agent's "apply for listing on the sub-agent marketplace" (pending admin review).

    Mirrors the skill marketplace's ``MarketplaceSubmission``, but snapshots a
    "sub-agent" rather than skill files: on submission, the source ``UserAgent`` is
    content-snapshotted (prompt/welcome message/suggested questions/model config/
    capability bindings); review and listing are both based on the snapshot — later
    edits or deletion of the original agent do not affect listed content.
    ``status='approved'`` records appear in the sub-agent marketplace list
    (source=community) and can be "install-cloned" by everyone as a private sub-agent;
    admins can reject at any time (including delisting already-listed items). Direct
    admin uploads to the marketplace are distinguished by the sentinel owner
    ``__admin_upload__``.
    """

    __tablename__ = "agent_market_submissions"

    submission_id = Column(String(64), primary_key=True)
    # Marketplace slug once listed (derived from name at submission, globally unique, never colliding with the preset marketplace catalog)
    slug = Column(String(128), nullable=False, unique=True)
    agent_id = Column(String(64), nullable=False)  # source UserAgent.agent_id
    owner_user_id = Column(String(64), nullable=False)  # applicant (or __admin_upload__)
    submitter_name = Column(
        String(255), default=""
    )  # applicant display name (denormalized for display)

    # Marketplace display metadata
    name = Column(String(255), nullable=False)
    avatar = Column(Text)
    description = Column(Text, default="")
    summary = Column(Text, default="")
    category = Column(String(64), default="通用助手")
    tags = Column(JSONType, default=list)
    version = Column(String(50), default="1.0.0")
    note = Column(Text, default="")  # application note (for the admin)

    # Sub-agent content snapshot (decoupled from the source UserAgent)
    system_prompt = Column(Text, default="")
    welcome_message = Column(Text, default="")
    suggested_questions = Column(JSONType, default=list)
    # {temperature, max_tokens, max_iters, timeout}
    model_config_snapshot = Column(JSONType, default=dict)
    # {skill_ids, mcp_server_ids, plugin_ids, kb_ids}
    bindings_snapshot = Column(JSONType, default=dict)

    status = Column(String(16), nullable=False, default="pending")
    review_note = Column(Text)  # rejection reason / review note
    reviewed_at = Column(UTCDateTime(timezone=True))
    created_at = Column(UTCDateTime(timezone=True), default=utc_now)
    updated_at = Column(UTCDateTime(timezone=True), default=utc_now, onupdate=utc_now)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="agent_market_submissions_status_check",
        ),
        Index("idx_agent_market_submissions_status", "status", "created_at"),
        Index("idx_agent_market_submissions_owner", "owner_user_id"),
    )
