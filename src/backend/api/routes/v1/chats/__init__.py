"""Chat API composition; static collection routes precede session-id routes."""

from api.routes.v1.chats import confirms as chat_confirms
from api.routes.v1.chats import feedback as chat_feedback
from api.routes.v1.chats import history as chat_history
from api.routes.v1.chats import questions as chat_questions
from api.routes.v1.chats import reruns as chat_reruns
from api.routes.v1.chats import run_views as chat_run_views
from api.routes.v1.chats import sessions as chat_sessions
from api.routes.v1.chats import sidebar as chat_sidebar
from fastapi import APIRouter

router = APIRouter(tags=["Sessions"])

router.include_router(chat_sidebar.router, prefix="/v1/chats")
router.include_router(chat_questions.router, prefix="/v1/chats")
router.include_router(chat_confirms.router, prefix="/v1/chats")
router.include_router(chat_run_views.router, prefix="/v1/chats")
router.include_router(chat_feedback.router, prefix="/v1/chats")
router.include_router(chat_history.router, prefix="/v1/chats")
router.include_router(chat_reruns.router, prefix="/v1/chats")
router.include_router(chat_sessions.router, prefix="/v1/chats")
