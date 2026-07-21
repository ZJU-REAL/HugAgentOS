"""SQLAlchemy ORM models.

Split into submodules by domain; this __init__ re-exports all model classes verbatim (plus Base/JSONType/INETType),
so `from core.db.models import X` and `from core.db.models import *` both stay unchanged.
"""

from core.db.engine import Base
from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import JSONB, INET

JSONType = JSON().with_variant(JSONB(), "postgresql")
INETType = String(45).with_variant(INET(), "postgresql")

from core.db.models.identity import UserShadow, LocalUser, InviteCode, Team, TeamMember, TeamFolder, UserFolder, UserApiKey, DingTalkConnection, LarkConnection, EmailConnection, ChannelConnection, Role, RoleAssignment
from core.db.models.project import Project, ProjectFavorite
from core.db.models.chat import ChatSession, ChatSessionUserState, ChatMessage, ChatRun, MessageFeedback, ChatSandboxSnapshot
from core.db.models.knowledge import KBSpace, KBDocument, KBChunk, CatalogOverride, KBGrant
from core.db.models.artifact import Artifact, ContentBlock
from core.db.models.config import ModelProvider, SystemConfig, ModelRoleAssignment, ModelPricing, GatewayVirtualKey
from core.db.models.admin import AdminSkill, SandboxRebuild, SkillDependencyRequest, AdminPromptPart, AdminMcpServer, AdminSkillDraft, MarketplaceSubmission, InstalledPlugin, PluginMarketPackage, PluginMarketSkillExclusion, MarketplaceListingState, MarketplaceVisibilityGrant
from core.db.models.agent import UserAgent, AgentMarketSubmission, Plan, PlanStep, AgentLoop, LoopIteration
from core.db.models.automation import ScheduledTask, ScheduledTaskRun, DistillationRun, PersonaDistillJob, BatchPlan
from core.db.models.logs import ToolCallLog, SubAgentCallLog, SkillCallLog, AuditLog
from core.db.models.memory import ProfileMemory, MemoryAudit, MemorySanitizerRule
from core.db.models.datasource import DataSource, DsTableMeta, DsColumnMeta, DsGoldenSql
from core.db.models.site import Site, SiteKV, SiteSubmission
from core.db.models.ontology import (
    OntologyDraft,
    OntologyEnforcementEvent,
    OntologyPack,
    OntologyPackVersion,
    OntologyReviewRun,
)

__all__ = [
    "Site",
    "SiteKV",
    "SiteSubmission",
    "DataSource",
    "DsTableMeta",
    "DsColumnMeta",
    "DsGoldenSql",
    "Base",
    "JSONType",
    "INETType",
    "UserShadow",
    "UserApiKey",
    "DingTalkConnection",
    "LarkConnection",
    "EmailConnection",
    "ChannelConnection",
    "LocalUser",
    "InviteCode",
    "Team",
    "TeamMember",
    "TeamFolder",
    "Role",
    "RoleAssignment",
    "UserFolder",
    "Project",
    "ProjectFavorite",
    "ChatSession",
    "ChatSessionUserState",
    "ChatMessage",
    "ChatRun",
    "MessageFeedback",
    "ChatSandboxSnapshot",
    "KBSpace",
    "KBDocument",
    "KBChunk",
    "CatalogOverride",
    "KBGrant",
    "Artifact",
    "ContentBlock",
    "ModelProvider",
    "SystemConfig",
    "ModelRoleAssignment",
    "ModelPricing",
    "GatewayVirtualKey",
    "AdminSkill",
    "SandboxRebuild",
    "SkillDependencyRequest",
    "AdminPromptPart",
    "AdminMcpServer",
    "AdminSkillDraft",
    "MarketplaceSubmission",
    "InstalledPlugin",
    "PluginMarketPackage",
    "PluginMarketSkillExclusion",
    "MarketplaceListingState",
    "MarketplaceVisibilityGrant",
    "UserAgent",
    "AgentMarketSubmission",
    "Plan",
    "PlanStep",
    "AgentLoop",
    "LoopIteration",
    "ScheduledTask",
    "ScheduledTaskRun",
    "DistillationRun",
    "PersonaDistillJob",
    "BatchPlan",
    "ToolCallLog",
    "SubAgentCallLog",
    "SkillCallLog",
    "AuditLog",
    "ProfileMemory",
    "MemoryAudit",
    "MemorySanitizerRule",
    "OntologyPack",
    "OntologyPackVersion",
    "OntologyEnforcementEvent",
    "OntologyReviewRun",
    "OntologyDraft",
]
