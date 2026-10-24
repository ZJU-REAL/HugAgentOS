"""Data access layer — Repository pattern.

Repositories are organised into domain submodules; this package re-exports
every public class so existing imports (``from core.db.repository import
XxxRepository``) keep working.
"""

from core.db.edition import UserAgentRepository
from core.db.repository.artifact import ROOT_FOLDER_SENTINEL, ArtifactRepository
from core.db.edition import AuditLogRepository
from core.db.repository.catalog import CatalogRepository

from core.db.repository.chat import ChatMessageRepository, ChatSessionRepository

from core.db.repository.ontology import OntologyRepository
from core.db.repository.user import LocalUserRepository, UserRepository





__all__ = [
    "UserRepository",
    "LocalUserRepository",
    "ChatSessionRepository",
    "ChatMessageRepository",
    "CatalogRepository",
    "ArtifactRepository",
    "ROOT_FOLDER_SENTINEL",
    "AuditLogRepository",
    "UserAgentRepository",
    "OntologyRepository",
]
