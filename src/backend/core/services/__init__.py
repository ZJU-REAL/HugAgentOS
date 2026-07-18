"""Business logic layer - Service classes.

Re-exports all service classes for backwards compatibility with
``from core.services import UserService`` etc.
"""

from core.services.user_service import UserService
from core.services.chat_service import ChatService
from core.services.catalog_service import CatalogService
from core.services.kb_service import KBService
from core.services.kb_permission_service import KBPermissionService
from core.services.artifact_service import ArtifactService
from core.services.user_agent_service import UserAgentService
from core.services.api_key_service import ApiKeyService
from core.services.role_service import RoleService

# Note: ProjectService / ProjectFileService are not exported here, because project_service.py
# depends on core.auth.project_permissions (which depends on core.auth.backend, and backend in turn
# imports this module → circular import). So import them directly via ``from core.services.project_service import
# ProjectService``.

__all__ = [
    "UserService",
    "ChatService",
    "CatalogService",
    "KBService",
    "KBPermissionService",
    "ArtifactService",
    "UserAgentService",
    "ApiKeyService",
    "RoleService",
]
