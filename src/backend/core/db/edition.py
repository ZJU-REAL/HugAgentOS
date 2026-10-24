"""Community database schema and personal resource ownership."""
from core.db.personal_model_fields import ProjectEditionFields, project_edition_table_args, ArtifactEditionFields, artifact_edition_table_args, UserAgentEditionFields, user_agent_edition_table_args, ChatSessionEditionFields, chat_session_edition_table_args, MarketplaceListingEditionFields, EDITION_MODEL_EXPORTS
from core.db.personal_schema import ce_create_all, ce_reconcile_schema

EDITION_MODEL_NAMES = ()


def personal_artifact_scope_fields() -> dict[str, None]:
    return {}


def __getattr__(name):
    if name == "UserAgentRepository":
        from core.db.repository.personal_agent import UserAgentRepository
        return UserAgentRepository
    if name == "AuditLogRepository":
        from core.db.repository.personal_audit import AuditLogRepository
        return AuditLogRepository
    raise AttributeError(name)


def personal_index_scope(enterprise=False):
    return ""


def edition_model_exports():
    return {}
