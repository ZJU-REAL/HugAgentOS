"""Run module-owned cleanup within the deleting session's transaction."""
from sqlalchemy import event
from sqlalchemy.orm import Session

from core.harness.registry.user_cleanup import cleanup_user


def install_user_cleanup(user_model):
    @event.listens_for(Session, "before_flush")
    def clean_deleted_users(session, flush_context, instances):
        for instance in tuple(session.deleted):
            if isinstance(instance, user_model):
                cleanup_user(session, instance)
