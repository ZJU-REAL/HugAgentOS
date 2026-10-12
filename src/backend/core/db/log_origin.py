"""Classify API provenance from server-owned admission records without copying logs."""

from core.db.models import ChatMessage, ChatRun, ChatSession
from sqlalchemy import case, exists, literal, or_, select
from sqlalchemy.orm import aliased


def api_log_predicate(model):
    run = aliased(ChatRun)
    scope = run.request_payload["agent_api_scope"]
    authenticated_api = or_(
        run.request_payload["request_origin"].as_string() == "api",
        (scope["api_key_id"].as_string().isnot(None))
        & (scope["owner_user_id"].as_string() == run.user_id),
    )
    conditions = [run.chat_id == model.chat_id, authenticated_api]
    if hasattr(model, "user_id"):
        conditions.append(run.user_id == model.user_id)
    else:
        session = aliased(ChatSession)
        conditions.append(
            exists(
                select(1)
                .where(session.chat_id == model.chat_id, session.user_id == run.user_id)
                .correlate(model, run)
            )
        )
    if hasattr(model, "message_id"):
        # Steer creates another assistant message in the same durable run.
        message = aliased(ChatMessage)
        continued = exists(
            select(1)
            .where(
                message.message_id == model.message_id,
                message.chat_id == model.chat_id,
                message.recorded_run_id == run.run_id,
            )
            .correlate(model, run)
        )
        conditions.append(or_(run.message_id == model.message_id, continued))
    return exists(select(1).where(*conditions).correlate(model))


def log_origin(model):
    return case((api_log_predicate(model), literal("api")), else_=literal("cloud"))


def annotate_log_origins(db, model, items):
    """One bounded query for a page or detail; origin does not change ownership."""
    if not items:
        return items
    primary = model.__mapper__.primary_key[0]
    key = primary.key
    ids = [item[key] for item in items]
    query = select(primary, log_origin(model)).where(primary.in_(ids))
    origins = dict(db.execute(query).all())
    return [{**item, "origin": origins.get(item[key], "cloud")} for item in items]
