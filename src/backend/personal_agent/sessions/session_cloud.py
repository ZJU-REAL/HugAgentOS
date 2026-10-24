"""Reuse shared session operations with the authenticated owner context."""

from fastapi import HTTPException
from hugagent_cli.contract import validate


async def execute(user, operation, args, origin=None):
    """Run one session operation as ``user``; database operations run in a worker thread."""
    args = validate(operation, args)
    if operation == "chat.send":
        from core.chat.schemas import ChatRequest
        from core.chat.operations.admission import start_response_run as _start_response_run

        run = await _start_response_run(ChatRequest(**args, stream=True), user)
        return {
            "run_id": run.run_id,
            "chat_id": run.chat_id,
            "message_id": run.message_id,
            "status": "accepted",
        }
    if operation == "run.status":
        from core.db.threads import run_db

        return await run_db(run_status, args["run_id"], user)
    if operation == "run.cancel":
        from core.chat.operations.commands import cancel_chat_run

        return (await cancel_chat_run(args["run_id"], user=user))["data"]
    if operation == "run.steer":
        from core.chat.operations.commands import steer_chat_run
        from core.chat.operations.schemas import SteerChatRunRequest

        result = await steer_chat_run(
            args["run_id"],
            SteerChatRunRequest(**{k: v for k, v in args.items() if k != "run_id"}),
            user=user,
        )
        return result["data"]
    from core.db.threads import run_in_session

    return await run_in_session(_execute_in_session, user, operation, args, origin)


def _execute_in_session(db, user, operation, args, origin):
    from core.chat.operations import history
    from core.chat.operations import runs as run_views
    from core.chat.operations import schemas as models
    from core.chat.operations import sessions

    if operation == "chat.list":
        result = sessions.list_chats(
            page=args.get("page", 1),
            page_size=args.get("page_size", 20),
            sort="-updated_at",
            filter=None,
            exclude_automation=False,
            user=user,
            db=db,
        )
    elif operation == "chat.search":
        result = sessions.search_chats(
            q=args["q"],
            scope=args.get("scope", "title"),
            page=args.get("page", 1),
            page_size=args.get("page_size", 20),
            user=user,
            db=db,
        )
    elif operation == "chat.create":
        # Mark chats that a Personal Agent task created, so the chat list can show their source.
        metadata = {"personal_agent_task_id": origin} if origin else {}
        result = sessions.create_chat(models.CreateChatRequest(**args, metadata=metadata), user=user, db=db)
    elif operation == "chat.get":
        result = sessions.get_chat(args["chat_id"], user=user, db=db)
    elif operation == "chat.update":
        result = sessions.update_chat(
            args["chat_id"],
            models.UpdateChatRequest(**{k: v for k, v in args.items() if k != "chat_id"}),
            user=user,
            db=db,
        )
    elif operation == "chat.delete":
        sessions.delete_chat(args["chat_id"], user=user, db=db)
        return {"deleted": True}
    elif operation == "chat.history":
        result = history.list_messages(
            chat_id=args["chat_id"],
            page=args.get("page", 1),
            page_size=args.get("page_size", 50),
            order=args.get("order", "desc"),
            cursor=False,
            before_seq=None,
            user=user,
            db=db,
        )
    elif operation == "run.active":
        result = run_views.chat_active_run(args["chat_id"], user=user, db=db)
    elif operation == "run.list":
        result = run_views.list_active_chat_runs(user=user, db=db)
    # Defaults match the HTTP routes that the device path calls.
    elif operation == "project.list":
        from core.chat.operations.projects import list_projects

        result = list_projects(
            q=args.get("q"),
            sort="-last_activity_at",
            page=args.get("page", 1),
            page_size=args.get("page_size", 30),
            user=user,
            db=db,
        )
    elif operation == "project.get":
        from core.chat.operations.projects import get_project

        result = get_project(access=project_access(args, user, db), db=db)
    elif operation == "project.chats":
        from core.chat.operations.projects import list_project_chats

        result = list_project_chats(
            page=args.get("page", 1),
            page_size=args.get("page_size", 50),
            scope="all",
            access=project_access(args, user, db),
            db=db,
        )
    return result["data"]

def project_access(args, user, db):
    """The HTTP routes' own view check, which also audits denials."""
    from core.auth.edition import require_project_access

    return require_project_access("view")(
        project_id=args["project_id"], request=None, user=user, db=db
    )

def run_status(ident, user):
    from core.runs.runtime import get_run

    row = get_run(ident)
    if row is None or row.user_id != user.user_id:
        raise HTTPException(404, "Run not found")
    return {
        "run_id": row.run_id,
        "chat_id": row.chat_id,
        "message_id": row.message_id,
        "status": row.status,
    }
