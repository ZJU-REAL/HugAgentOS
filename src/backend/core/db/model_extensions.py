"""Community ORM extensions: personal-resource schema only."""

from sqlalchemy import CheckConstraint


class ProjectEditionFields:
    pass


def project_edition_table_args() -> tuple:
    return (CheckConstraint("kind = 'personal'", name="ck_projects_kind_personal"),)


class ArtifactEditionFields:
    pass


def artifact_edition_table_args() -> tuple:
    return ()


class UserAgentEditionFields:
    pass


def user_agent_edition_table_args() -> tuple:
    return (
        CheckConstraint(
            "owner_type IN ('admin', 'user')",
            name="user_agents_owner_type_check",
        ),
    )


class ChatSessionEditionFields:
    pass


def chat_session_edition_table_args() -> tuple:
    return ()


class MarketplaceListingEditionFields:
    pass


EDITION_MODEL_EXPORTS = {}


__all__ = [
    "ArtifactEditionFields",
    "ChatSessionEditionFields",
    "EDITION_MODEL_EXPORTS",
    "MarketplaceListingEditionFields",
    "ProjectEditionFields",
    "UserAgentEditionFields",
    "artifact_edition_table_args",
    "chat_session_edition_table_args",
    "project_edition_table_args",
    "user_agent_edition_table_args",
]
