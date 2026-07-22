"""Shared helpers for the new file-operation tools."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import shlex
from typing import Any, Optional

from agentscope.message import TextBlock

# AgentScope 2.0: tool functions must return ToolChunk; aliased (its fields are a superset of ToolResponse).
from agentscope.tool._response import ToolChunk as ToolResponse
from core.llm.tools.edition_myspace_vfs import organization_mutation_blocked
from core.services.project_scope import ProjectScope

logger = logging.getLogger(__name__)


def resp_json(payload: dict[str, Any]) -> ToolResponse:
    """Wrap a JSON dict as a single-text-block ToolResponse.

    Mirrors ``core.llm.tool._resp_json``. Re-implemented here so this package
    has zero cross-imports back to the large monolithic ``tool.py``.
    """
    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )
        ]
    )


def resolve_sandbox_session(
    sandbox_session_id: Optional[str],
    chat_id: Optional[str],
) -> Optional[str]:
    """``sandbox_session_id`` wins; ``None`` means 'unspecified' → fall back to
    ``chat_id`` (legacy behavior). Explicit ``""`` stays ephemeral."""
    return chat_id if sandbox_session_id is None else sandbox_session_id


async def myspace_write_guard(
    *,
    chat_id: Optional[str],
    op: str,
    logical_path: str,
    is_myspace: bool,
    interactive: bool,
    summary: str,
) -> Optional[ToolResponse]:
    """§13 gate (Claude Code shape): an unconfirmed /myspace write **suspends the
    current tool coroutine** to wait for the user's out-of-band decision; approve
    → return None to let it through (the caller performs the write once in place),
    reject/timeout/non-interactive → return an intercepting ToolResponse (the
    caller returns it directly).

    NOTE: this function ``await``s — the caller must ``await myspace_write_guard(...)``.
    While suspended it only pauses the agent task, it does not block the event
    loop / SSE (see the _myspace_confirm header note).
    """
    # Non-/myspace writes (temporary sandbox artifacts etc.) are not gated — this
    # is an admission decision unique to the myspace flow, kept here in the caller
    # layer so the generic gate() stays kind-agnostic.
    if not is_myspace:
        return None
    from core.llm.tools import _myspace_confirm as _mc

    blk = await _mc.gate(
        chat_id=chat_id,
        op=op,
        logical_path=logical_path,
        interactive=interactive,
        summary=summary,
    )
    return resp_json(blk) if blk is not None else None


async def sandbox_exec_bash(
    script: str,
    *,
    chat_id: Optional[str],
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Run a bash script in the sandbox and return ``(exit_code, stdout, stderr)``.

    Wraps ``SandboxProvider.execute`` for the Glob/Grep tools. Errors are
    surfaced as ``(exit_code=-1, stdout="", stderr=str(exc))``.

    NOTE: ``chat_id`` here is the *sandbox session id* — callers pass the
    resolved ``_sess`` (``sandbox_session_id`` or chat_id fallback), never a
    DB-scoping chat id. Kept named ``chat_id`` to avoid churning call sites.
    """
    from core.sandbox import ExecuteRequest, SandboxConnectError, SandboxError, get_sandbox_provider

    try:
        provider = get_sandbox_provider()
        result = await provider.execute(
            ExecuteRequest(
                script_content=script,
                script_name="_tool_helper.sh",
                language="bash",
                timeout=max(1, min(int(timeout or 30), 60)),
                session_id=chat_id,
            )
        )
        return result.exit_code, result.stdout, result.stderr
    except (SandboxError, SandboxConnectError) as exc:
        return -1, "", str(exc)


def shell_quote(value: str) -> str:
    """Shell-quote a value for safe interpolation into a bash command."""
    return shlex.quote(value)


def upsert_myspace_artifact(
    *,
    user_id: str,
    chat_id: Optional[str],
    filename: str,
    content: bytes,
    scope: Optional[ProjectScope] = None,
) -> Optional[dict[str, Any]]:
    """Sync ``filename`` (under the user's myspace) to artifact storage in place.

    Behavior:
      - Looks up the most recent live artifact for this user with the same
        ``filename`` (chat_id-independent — myspace files persist across chats).
      - If found: re-upload bytes to its existing ``storage_key``, refresh
        ``size_bytes`` / ``updated_at`` on the DB row. Reuses the same
        ``artifact_id`` (a.k.a. file_id) — so Canvas / pin / download URLs stay
        valid across edits.
      - If not found: register a new artifact via the legacy
        ``_store_generated_files`` path (which writes the JSON index + storage).
        The DB row gets inserted by ``_persist_collected_artifacts`` at the end
        of the chat run when the returned ref bubbles up.
      - In **both** cases, mirror the bytes into ``myspace_cache/{user_id}/``
        so the next sandbox's seed step picks up the change.

    Returns the artifact ref dict (``{file_id, name, url, mime_type, size,
    storage_key, in_place_update}``) on success, or ``None`` if registration
    fails (e.g. storage backend down). Callers should surface ``None`` as a
    soft warning, not block the Write/Edit itself.
    """
    if not user_id or not filename:
        logger.warning("[artifact-sync] missing user_id or filename; skip")
        return None

    if organization_mutation_blocked(scope):
        logger.info(
            "[artifact-sync] edition scope blocked auto-upsert for filename=%s",
            filename,
        )
        return None

    name = filename.rsplit("/", 1)[-1] or "output"
    mime, _ = mimetypes.guess_type(name)
    mime = mime or "application/octet-stream"

    # ── 1. Mirror to myspace_cache so next sandbox seed sees the update ──
    try:
        from core.sandbox._common import myspace_cache_dir

        cache_dir = myspace_cache_dir(user_id)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / name).write_bytes(content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[artifact-sync] myspace_cache write failed: %s", exc)
        # Continue — DB/storage is still useful even if cache write fails

    # ── 2. Try in-place update of an existing live artifact ────────────
    try:
        from datetime import datetime, timezone

        from core.db.engine import SessionLocal
        from core.db.models import Artifact
        from core.storage import get_storage
    except Exception as exc:  # noqa: BLE001
        logger.warning("[artifact-sync] deps unavailable: %s", exc)
        return None

    db = SessionLocal()
    try:
        row = (
            db.query(Artifact)
            .filter(
                Artifact.user_id == user_id,
                Artifact.filename == name,
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc())
            .first()
        )
        if row is not None:
            try:
                storage = get_storage()
                storage.upload_bytes(content, str(row.storage_key))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[artifact-sync] upload_bytes failed for %s: %s",
                    row.storage_key,
                    exc,
                )
                return None
            row.size_bytes = max(len(content), 1)
            row.updated_at = datetime.now(timezone.utc)
            row.mime_type = mime  # filename might be same but content type changed
            db.commit()
            logger.info(
                "[artifact-sync] in-place updated artifact %s (user=%s name=%s size=%d)",
                row.artifact_id,
                user_id,
                name,
                len(content),
            )
            return {
                "file_id": row.artifact_id,
                "name": name,
                "url": f"/files/{row.artifact_id}",
                "mime_type": mime,
                "size": len(content),
                "storage_key": row.storage_key,
                "in_place_update": True,
            }
    finally:
        db.close()

    # ── 3. No existing artifact → register a new one ───────────────────
    # _store_generated_files writes to storage + the JSON index, but does NOT
    # insert into the DB ``artifacts`` table. That insert is normally done by
    # ``_persist_collected_artifacts`` at the end of a chat run, by collecting
    # tool refs.
    #
    # Problem: within the SAME chat run, a 2nd call to upsert_myspace_artifact
    # for the same filename (e.g. Write → Edit) needs the DB row to exist so
    # the in-place branch above can find it. Without an immediate DB insert,
    # consecutive Write/Edit on the same file would create N artifacts instead
    # of 1, defeating the entire in-place design.
    #
    # So: we **also** insert the DB row right here. End-of-run dedup in
    # _persist_collected_artifacts already skips by artifact_id, so this won't
    # cause double inserts.
    try:
        from core.llm.tools._tool_helpers import _store_generated_files
    except Exception as exc:  # noqa: BLE001
        logger.warning("[artifact-sync] _store_generated_files unavailable: %s", exc)
        return None

    refs = _store_generated_files(
        [
            {
                "name": name,
                "size": len(content),
                "content_b64": base64.b64encode(content).decode("ascii"),
                "mime_type": mime,
            }
        ],
        user_id=user_id,
        source="myspace_sync",
        extra_metadata={"chat_id": chat_id} if chat_id else None,
    )
    if not refs:
        return None
    ref = dict(refs[0])
    ref["in_place_update"] = False
    new_file_id = ref.get("file_id")

    # Insert DB row immediately so future in-place lookups (same run, same chat)
    # find this artifact. Skip if no chat_id (FK requires it).
    if new_file_id and chat_id:
        try:
            from datetime import datetime, timezone

            from core.db.engine import SessionLocal
            from core.db.models import Artifact
        except Exception as exc:  # noqa: BLE001
            logger.warning("[artifact-sync] DB insert deps unavailable: %s", exc)
        else:
            # Project-mode auto-placement: under a personal scope, a newly
            # registered artifact lands under the project-linked folder. The
            # register_as_artifact path carries no logical path, so sync_upsert's
            # myspace_rel prefix cannot cover it — we must explicitly set folder here.
            _proj_folder_id: Optional[str] = (
                scope.root_folder_id if scope is not None and scope.is_personal else None
            )
            db = SessionLocal()
            try:
                # Guard against race / duplicate
                existing = (
                    db.query(Artifact)
                    .filter(
                        Artifact.artifact_id == new_file_id,
                    )
                    .first()
                )
                if existing is None:
                    db.add(
                        Artifact(
                            artifact_id=new_file_id,
                            chat_id=chat_id,
                            user_id=user_id,
                            user_folder_id=_proj_folder_id,
                            type="other",
                            title=name,
                            filename=name,
                            size_bytes=max(len(content), 1),
                            mime_type=mime,
                            storage_key=ref.get("storage_key") or f"artifacts/{new_file_id}",
                            storage_url=ref.get("url"),
                            extra_data={"source": "myspace_sync"},
                        )
                    )
                    db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[artifact-sync] DB insert failed for %s: %s", new_file_id, exc)
                db.rollback()
            finally:
                db.close()

    logger.info(
        "[artifact-sync] new artifact %s registered (user=%s name=%s)",
        new_file_id,
        user_id,
        name,
    )
    return ref


def pin_artifact_to_workspace(ref: dict[str, Any]) -> bool:
    """Pin an artifact ref into the per-run workspace state.

    The frontend's "attachment card" rendering is gated by ``workspace_files``
    (see MessageBubble.renderArtifactCards). Without an explicit pin, an
    in-place Edit or Write produces no visible card in the current turn —
    the file_id is the same as before, ``workspaceFiles`` is empty for this
    turn, and the user has no visual confirmation of the change.

    Auto-pinning every successful myspace upsert solves the UX gap: a fresh
    card always appears in the same turn as the Edit/Write.
    """
    file_id = ref.get("file_id") if isinstance(ref, dict) else None
    if not file_id:
        return False
    try:
        from core.llm import workspace as _workspace

        pinned = _workspace.pin(
            file_id=str(file_id),
            name=ref.get("name"),
            mime_type=ref.get("mime_type"),
            size=ref.get("size"),
            url=ref.get("url"),
        )
        if pinned:
            _workspace.mark_active()
        return pinned
    except Exception as exc:  # noqa: BLE001
        logger.warning("[artifact-sync] pin_to_workspace failed for %s: %s", file_id, exc)
        return False
