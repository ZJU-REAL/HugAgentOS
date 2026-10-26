"""Users' self-service private remote MCPs (header or OAuth authenticated).

Shared by the plain "connect private MCP" route and the browser OAuth flow,
which saves the same row only after the user has granted consent.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from core.capabilities.mcp.management import encrypt_mcp_headers, refresh_mcp_caches
from core.db.models import AdminMcpServer
from core.infra.time import utc_now


def build_private_server(
    *,
    owner_user_id: str,
    display_name: str,
    description: str,
    user_intro: Optional[str],
    transport: str,
    url: str,
    headers: Dict[str, str],
    icon: Optional[str],
) -> AdminMcpServer:
    """A transient, plaintext-header row; persist it with ``save_private_server``."""
    now = utc_now()
    return AdminMcpServer(
        # Globally unique so it never collides with public MCPs or other users.
        server_id=f"umcp_{uuid.uuid4().hex[:16]}",
        display_name=display_name.strip(),
        description=description or "",
        user_intro=user_intro,
        transport=transport,
        url=url.strip(),
        headers=dict(headers or {}),
        is_enabled=True,
        owner_user_id=owner_user_id,
        icon=icon,
        created_at=now,
        updated_at=now,
        created_by=owner_user_id,
    )


def private_server_dict(row: AdminMcpServer) -> Dict[str, Any]:
    return {
        "server_id": row.server_id,
        "display_name": row.display_name,
        "transport": row.transport,
        "url": row.url,
        "owner": "self",
    }


async def save_private_server(row: AdminMcpServer, headers: Dict[str, str]) -> Dict[str, Any]:
    """Encrypt ``headers`` onto a probed row and commit it."""
    from core.db.threads import run_in_session

    row.headers = encrypt_mcp_headers(headers)

    def save(db) -> None:
        db.add(row)
        db.commit()
        db.refresh(row)

    await run_in_session(save)
    refresh_mcp_caches()
    return private_server_dict(row)
