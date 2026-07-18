"""MemoryContext — the unified context carrier for memory operations.

Carries user_id / workspace_id / chat_id / the sensitivity levels readable by the
current user; every memory read/write/audit interface receives it, keeping the
parameter lists from sprawling across the project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


Confidentiality = Literal["public", "internal", "sensitive"]


@dataclass(frozen=True)
class MemoryContext:
    """Memory access context.

    Attributes:
        user_id: id of the requesting user
        workspace_id: workspace id (can represent an org/department/project/"default").
            Government scenarios use the office id, enterprise scenarios the
            department id, personal SaaS is always "default".
        chat_id: optional, current chat id (used for audit replay)
        allowed_levels: sensitivity levels readable by the current role; defaults to
            ["public", "internal", "sensitive"] (a user reads all of their own);
            admins viewing others' memories should shrink it to ["public", "internal"]
        confidentiality: sensitivity of the memory entry currently being operated on
            (optional, audit only)
        actor: initiator of the operation, defaults to user_id; may be "system" for
            automated system operations
    """

    user_id: str
    workspace_id: str = "default"
    chat_id: Optional[str] = None
    allowed_levels: tuple[Confidentiality, ...] = ("public", "internal", "sensitive")
    confidentiality: Optional[Confidentiality] = None
    actor: Optional[str] = None
    # Permanent memory is written only after the user's explicit consent (off by
    # default, must be turned on explicitly); the read switch is decided by the
    # caller at the workflow layer (whether to invoke the retrieve path) and is
    # not part of ctx
    write_enabled: bool = False
    # mem0 scope identifier. Under a team project = "team:<team_id>", all members
    # share reads and writes; default / personal projects = None, falling back to
    # user_id. audit / metadata.author_user_id still record the real user_id so
    # audits stay traceable.
    scope_user_id: Optional[str] = None

    @property
    def effective_actor(self) -> str:
        return self.actor or self.user_id

    @property
    def effective_scope_user_id(self) -> str:
        """The user_id passed into mem0. Team scope under team projects, otherwise the real user."""
        return self.scope_user_id or self.user_id

    def with_confidentiality(self, level: Confidentiality) -> "MemoryContext":
        """Return a copy that differs only in the confidentiality field."""
        return MemoryContext(
            user_id=self.user_id,
            workspace_id=self.workspace_id,
            chat_id=self.chat_id,
            allowed_levels=self.allowed_levels,
            confidentiality=level,
            actor=self.actor,
            write_enabled=self.write_enabled,
            scope_user_id=self.scope_user_id,
        )


def resolve_workspace_id(request_ctx) -> str:
    """Default workspace resolver, returns "default".

    Government/enterprise multi-tenant deployments may override this function in
    `core/llm/memory_context_overrides.py` (create it yourself), e.g.:

        from core.auth.session import get_current_user
        user = get_current_user(request_ctx)
        return user.department_id or user.org_id or "default"

    In the first release, all deployments use the default, keeping behavior
    unchanged from the status quo.
    """
    return "default"


def resolve_allowed_levels(user_id: str, workspace_id: str) -> tuple[Confidentiality, ...]:
    """Default implementation: when a user reads their own memories, all levels are readable.

    Admin interfaces viewing others' memories should explicitly pass
    `("public", "internal")`; `sensitive` requires secondary authorization.
    """
    return ("public", "internal", "sensitive")
