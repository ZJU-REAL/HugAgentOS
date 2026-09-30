"""Observe committed domain changes; rollback never publishes a projection refresh."""

import logging

from sqlalchemy import event, select
from sqlalchemy.orm import Session

_subscriptions = []


def subscribe(model, owner, callback):
    subscription = (model, owner, callback)
    _subscriptions.append(subscription)

    def unsubscribe():
        if subscription in _subscriptions:
            _subscriptions.remove(subscription)

    return unsubscribe


@event.listens_for(Session, "after_flush")
def collect(session, context):
    transaction = session.get_nested_transaction() or session.get_transaction()
    notifications = session.info.setdefault("space_projection_notifications", {}).setdefault(
        transaction, set()
    )
    for row in set(session.new) | set(session.dirty) | set(session.deleted):
        for model, owner, callback in tuple(_subscriptions):
            if isinstance(row, model):
                identity = owner(row)
                if identity:
                    notifications.add((callback, str(identity)))


@event.listens_for(Session, "after_commit")
def publish(session):
    notifications = session.info.get("space_projection_notifications", {})
    if session.in_nested_transaction():
        nested = session.get_nested_transaction()
        notifications.setdefault(session.get_transaction(), set()).update(
            notifications.pop(nested, set())
        )
        return
    session.info.pop("space_projection_notifications", None)
    for callback, identity in set().union(*notifications.values()):
        try:
            callback(identity)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Committed space refresh notification failed: %s", type(exc).__name__
            )


@event.listens_for(Session, "after_rollback")
def discard(session):
    if session.in_nested_transaction():
        session.info.get("space_projection_notifications", {}).pop(
            session.get_nested_transaction(), None
        )
    else:
        session.info.pop("space_projection_notifications", None)


@event.listens_for(Session, "do_orm_execute")
def collect_bulk_changes(state):
    """Bulk ORM mutations bypass after_flush; capture their affected owners first."""
    if not (state.is_update or state.is_delete) or state.bind_mapper is None:
        return
    model = state.bind_mapper.class_
    subscriptions = [item for item in tuple(_subscriptions) if item[0] is model]
    if not subscriptions:
        return
    query = select(model)
    predicate = state.statement.whereclause
    if predicate is not None:
        query = query.where(predicate)
    parameters = (
        state.parameters if isinstance(state.parameters, list) else [state.parameters or {}]
    )
    rows = [row for values in parameters for row in state.session.execute(query, values).scalars()]
    transaction = state.session.get_nested_transaction() or state.session.get_transaction()
    notifications = state.session.info.setdefault("space_projection_notifications", {}).setdefault(
        transaction, set()
    )
    for row in rows:
        for _, owner, callback in subscriptions:
            identity = owner(row)
            if identity:
                notifications.add((callback, str(identity)))
