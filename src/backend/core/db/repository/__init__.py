"""Data access layer — Repository pattern.

Repositories are organised into domain submodules; this package re-exports
every public class so existing imports (``from core.db.repository import
XxxRepository``) keep working.
"""

from core.db.repository.user import UserRepository, LocalUserRepository, DingTalkConnectionRepository, LarkConnectionRepository, EmailConnectionRepository
from core.db.repository.chat import ChatSessionRepository, ChatMessageRepository
from core.db.repository.catalog import CatalogRepository
from core.db.repository.kb import KBRepository, KBGrantRepository
from core.db.repository.artifact import ArtifactRepository, ROOT_FOLDER_SENTINEL
from core.db.repository.audit import AuditLogRepository
from core.db.repository.agent import UserAgentRepository
from core.db.repository.team import TeamRepository, InviteCodeRepository
from core.db.repository.role import RoleRepository
from core.db.repository.channel import ChannelConnectionRepository
from core.db.repository.site import SiteRepository

__all__ = [
    "UserRepository", "LocalUserRepository", "DingTalkConnectionRepository",
    "LarkConnectionRepository", "EmailConnectionRepository",
    "ChatSessionRepository", "ChatMessageRepository",
    "CatalogRepository", "KBRepository", "KBGrantRepository",
    "ArtifactRepository", "ROOT_FOLDER_SENTINEL",
    "AuditLogRepository", "UserAgentRepository",
    "TeamRepository", "InviteCodeRepository",
    "RoleRepository",
    "ChannelConnectionRepository",
    "SiteRepository",
]
