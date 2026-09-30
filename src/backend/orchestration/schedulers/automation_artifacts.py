"""Load pinned scheduled-run deliverables for channel delivery."""

from typing import List, Tuple
from core.infra.logging import get_logger

logger = get_logger(__name__)


def load_run_files(chat_id: str) -> List[Tuple[bytes, str, str]]:
    """Load the artifact files an automation run generated under its chat_id; returns a list of (bytes, filename, mime).

    Each run uses a brand-new chat_id (see _execute_*_task), and only pinned artifacts
    land in the Artifact table, so whatever is fetched by chat_id is exactly this run's
    deliverables. Used by channel delivery (Feishu etc.) to actually send out the
    generated documents. Best-effort.
    """
    from core.db.engine import SessionLocal
    from core.db.models import Artifact
    from core.storage import get_storage

    out: List[Tuple[bytes, str, str]] = []
    try:
        with SessionLocal() as db:
            storage = get_storage()
            rows = db.query(Artifact).filter(Artifact.chat_id == chat_id).all()
            for row in rows:
                if not row.storage_key:
                    continue
                try:
                    content = storage.download_bytes(row.storage_key)
                except Exception:  # noqa: BLE001
                    logger.warning("[scheduler] 产物下载失败 %s", row.artifact_id, exc_info=True)
                    continue
                out.append(
                    (
                        content,
                        row.filename or row.artifact_id,
                        row.mime_type or "application/octet-stream",
                    )
                )
    except Exception:  # noqa: BLE001
        logger.warning("[scheduler] 加载 run 产物失败 chat_id=%s", chat_id, exc_info=True)
    return out
