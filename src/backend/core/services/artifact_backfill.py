import logging
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from core.db.models import Artifact, ChatMessage, ChatSession
from core.content.artifact_refs import extract_file_refs, infer_artifact_type, require_artifact_storage_key
from core.services.artifact_edition import artifact_scope_fields

logger = logging.getLogger(__name__)
_BACKFILL_BATCH = 500

def _backfill_artifacts_from_messages(user_id: str, db: Session) -> int:
    """Scan historical messages for file references not yet in the Artifact table.

    Source priority depends on whether the message was written under the
    strict ``pin_to_workspace`` regime:

      • Strict-mode message (``extra_data.workspace_files`` is present —
        even as ``[]``): trust ``extra_data.artifacts`` only. Do NOT
        scrape ``tool_calls[].result``, since those carry every transient
        docx the agent emitted but did not pin.
      • Legacy message (no ``workspace_files`` field): fall back to
        scraping ``tool_calls[].result`` for AI-generated files so old
        chats don't lose their files. Plus ``extra_data.artifacts``.

    User attachments (``extra_data.attachments``) are always scraped —
    those represent files the user explicitly uploaded.
    """
    # Pass A — stream the messages and collect candidate refs. A heavy account
    # carries hundreds of MB of ``tool_calls``/``metadata`` JSON, so hold at most a
    # batch at a time. Nothing else may touch this Session while the server-side
    # cursor is open: scope lookups and inserts are deferred to pass B.
    rows = (
        db.query(
            ChatMessage.chat_id,
            ChatMessage.role,
            ChatMessage.tool_calls,
            ChatMessage.extra_data,
            ChatSession.project_id,
        )
        .join(ChatSession, ChatMessage.chat_id == ChatSession.chat_id)
        .filter(
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
            ChatMessage.role.in_(["assistant", "user"]),
        )
        .yield_per(_BACKFILL_BATCH)
    )

    pending: List[Tuple[str, Optional[str], Dict[str, Any]]] = []
    candidate_ids: set = set()
    for chat_id, role, tool_calls_col, extra_data, project_id in rows:
        file_refs: List[Dict[str, Any]] = []
        is_strict_message = (
            isinstance(extra_data, dict) and extra_data.get("workspace_files") is not None
        )

        # Source 1: tool_calls[].result (legacy assistant messages only)
        if role == "assistant" and not is_strict_message:
            for tc in tool_calls_col or []:
                file_refs.extend(extract_file_refs(tc.get("result")))

        if isinstance(extra_data, dict):
            # Source 2: extra_data.artifacts — pinned-only under strict mode
            for art in extra_data.get("artifacts") or []:
                file_refs.extend(extract_file_refs(art))

            # Source 3: extra_data.attachments (user uploads — always)
            for att in extra_data.get("attachments") or []:
                file_refs.extend(extract_file_refs(att))

        for ref in file_refs:
            # The same id shows up across sources and across messages; pass B keeps
            # only the first, so holding the rest just costs memory.
            if ref["file_id"] in candidate_ids:
                continue
            candidate_ids.add(ref["file_id"])
            pending.append((chat_id, project_id, ref))

    if not pending:
        return 0

    # Dedup against the GLOBAL artifact_id space, not this user's rows:
    # ``Artifact.artifact_id`` is a single-column global primary key, so a
    # content-hash file id already owned by another user/chat would otherwise
    # slip past a user-filtered set and blow up the whole INSERT batch. Look the
    # candidates up by id instead of pulling every id in the table — that scan
    # grows with the whole install, not with what this user actually references.
    existing_ids: set = set()
    candidates = list(candidate_ids)
    for i in range(0, len(candidates), _BACKFILL_BATCH):
        existing_ids.update(
            row[0]
            for row in db.query(Artifact.artifact_id).filter(
                Artifact.artifact_id.in_(candidates[i : i + _BACKFILL_BATCH])
            )
        )

    # Pass B — insert what is missing. A chat with no project can only ever
    # resolve to the empty scope, so skip the lookup entirely for those; it used
    # to cost one ChatSession round-trip per referenced message.
    from core.services.project_scope import (  # local: avoid top-level cycle
        project_scope_from_chat_id,
    )

    rootless_fields = artifact_scope_fields(None)
    scope_cache: Dict[str, Dict[str, Optional[str]]] = {}

    created = 0
    unresolved = False
    for chat_id, project_id, ref in pending:
        fid = ref["file_id"]
        if fid in existing_ids:
            continue
        if not project_id:
            scope_fields = rootless_fields
        elif project_id in scope_cache:
            scope_fields = scope_cache[project_id]
        else:
            scope_fields = artifact_scope_fields(project_scope_from_chat_id(db, chat_id))
            scope_cache[project_id] = scope_fields
        # Per-row SAVEPOINT: a residual collision (cross-process race, or a
        # duplicate id we couldn't see) rolls back ONLY this row, never the
        # whole batch. Plain ``db.add`` + a single trailing commit would let
        # one bad row abort the entire transaction (the old behaviour).
        existing_ids.add(fid)  # claim before insert so dup refs in-batch skip
        try:
            with db.begin_nested():
                db.add(
                    Artifact(
                        artifact_id=fid,
                        chat_id=chat_id,
                        user_id=user_id,
                        type=infer_artifact_type(ref["mime_type"]),
                        title=ref["name"],
                        filename=ref["name"],
                        size_bytes=max(ref.get("size", 0) or 0, 1),
                        mime_type=ref["mime_type"],
                        storage_key=require_artifact_storage_key(
                            fid, ref.get("storage_key"), filename=ref["name"], size=ref.get("size", 0)
                        ),
                        storage_url=ref.get("url", ""),
                        extra_data={"source": "backfill"},
                        **scope_fields,
                    )
                )
            created += 1
        except IntegrityError:
            logger.debug("backfill skip dup %s", fid, exc_info=True)
        except Exception:
            unresolved = True
            logger.warning("backfill storage reference remains unresolved artifact=%s", fid)

    if created:
        try:
            db.commit()
            logger.info("backfill_artifacts: created %d for user %s", created, user_id)
        except Exception:
            # Re-raise: the caller must not stamp the "already backfilled" marker
            # on a run whose rows never landed, or the user loses them for good.
            db.rollback()
            raise
    if unresolved:
        raise RuntimeError("Historical artifact references remain unresolved; retry required")
    return created
