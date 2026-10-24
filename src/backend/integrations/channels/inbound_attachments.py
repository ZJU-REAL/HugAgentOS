"""Channel attachments responsibilities."""
from __future__ import annotations
import mimetypes
from typing import Any, Dict, List
from integrations.channels.protocol import InboundMsg
from core.db.engine import SessionLocal
from core.db.models import Artifact
from core.db.threads import run_in_session
from integrations.channels.models import ChannelConnection
import logging
logger = logging.getLogger(__name__)
from integrations.channels import inbound_state

def _store(db, owner_id, content, name, mime, chat_id, channel_id) -> str:
    from core.files.artifacts.service import store_bytes_as_artifact

    art = store_bytes_as_artifact(
        db, user_id=owner_id, content=content, filename=name, mime_type=mime,
        chat_id=chat_id, source="channel_upload",
        extra={"channel_id": channel_id},
    )
    return art.artifact_id


async def ingest_attachments(
    adapter, conn: ChannelConnection, owner_id: str, chat_id: str, msg: InboundMsg
) -> List[Dict[str, Any]]:
    """Download inbound attachments → store as Artifacts (mirrors /v1/file/upload) → return uploaded_files items.

    Each item contains metadata only. Parsing is deferred to the shared
    file-context/read_artifact path and cached on first use.
    """
    download = getattr(adapter, "download_resource", None)
    if download is None or not msg.attachments:
        return []

    out: List[Dict[str, Any]] = []
    for att in msg.attachments:
        try:
            content = await download(conn, msg, att)
            if not content or len(content) > inbound_state.MAX_INBOUND_FILE_BYTES:
                continue
            name = att.get("name") or "file.bin"
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            artifact_id = await run_in_session(
                _store, owner_id, content, name, mime, chat_id, conn.channel_id
            )
            out.append({
                "file_id": artifact_id,
                "download_url": f"/files/{artifact_id}",
                "name": name,
                "mime_type": mime,
            })
        except Exception:  # noqa: BLE001
            logger.exception("[channels] 入站附件处理失败 key=%s", att.get("key"))
    return out


def load_generated_files(artifacts: List[Dict[str, Any]]):
    """Read the bytes of artifact files generated this turn; returns a list of (content, name, mime) for pushing back (best-effort)."""
    if not artifacts:
        return []
    from core.files.storage import get_storage

    files = []
    with SessionLocal() as db:
        storage = get_storage()
        for art in artifacts:
            fid = art.get("file_id")
            if not fid:
                continue
            row = db.query(Artifact).filter(Artifact.artifact_id == fid).first()
            if row is None or not row.storage_key:
                continue
            try:
                content = storage.download_bytes(row.storage_key)
            except Exception:  # noqa: BLE001
                logger.warning("[channels] 产物下载失败 %s", fid, exc_info=True)
                continue
            files.append((content, row.filename or art.get("name") or fid,
                          row.mime_type or "application/octet-stream"))
    return files
