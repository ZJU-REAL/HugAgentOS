"""Request schemas shared by the chat endpoint modules."""

from typing import List, Optional

from core.llm.tools.user_questions import MAX_QUESTIONS as USER_QUESTION_MAX
from pydantic import BaseModel, Field


class CreateChatRequest(BaseModel):
    """Request model for creating a chat session."""

    title: Optional[str] = Field("新对话", description="Chat session title")
    metadata: Optional[dict] = Field(default_factory=dict, description="Additional metadata")


class UpdateChatRequest(BaseModel):
    """Request model for updating a chat session."""

    title: Optional[str] = Field(None, description="Chat session title")
    pinned: Optional[bool] = Field(None, description="Pin status")
    favorite: Optional[bool] = Field(None, description="Favorite status")
    metadata: Optional[dict] = Field(None, description="Additional metadata")


class UpdateSidebarOrderRequest(BaseModel):
    """Request model for persisting the sidebar's manual (drag-and-drop) order."""

    order: List[str] = Field(default_factory=list, description="Chat ids in manual order")


class UserQuestionAnswerItem(BaseModel):
    """One browser answer correlated to the model's stable question id."""

    id: str = Field(..., min_length=1, max_length=64)
    # No numeric cap: which option ids are legal for this question — and how
    # many a multi-select may carry — is decided against the pending question
    # itself in ``user_questions.normalize_answers``.
    selected: List[str] = Field(default_factory=list)
    custom: Optional[str] = Field(None, max_length=2000)
    skipped: bool = False


class UserQuestionAnswerBody(BaseModel):
    # The ceiling is the domain's own question cap, imported rather than
    # restated: an answer set must match the pending questions one-for-one, so
    # a locally-invented bound here silently rejects otherwise valid rounds.
    answers: List[UserQuestionAnswerItem] = Field(..., min_length=1, max_length=USER_QUESTION_MAX)


class RegenerateRequest(BaseModel):
    """Request body for regenerating an assistant response."""

    message_index: int = Field(
        ..., description="0-based index of the assistant message in the chat"
    )


class EditAndResendRequest(BaseModel):
    """Request body for editing a user message and regenerating."""

    message_index: int = Field(..., description="0-based index of the user message in the chat")
    new_content: str = Field(
        ..., min_length=1, max_length=10000, description="New content for the user message"
    )


class FeedbackRequest(BaseModel):
    rating: str
    comment: Optional[str] = None
    chat_id: Optional[str] = None


class FileConfirmBody(BaseModel):
    confirm_id: str = Field(..., description="工具返回的 awaiting confirm_id")
    decision: str = Field(
        ..., description="allow | allow_session | deny | choice | skip（后两者为建站设计三选一）"
    )
    option_id: Optional[str] = Field(None, description="decision=choice 时必填：选中的设计方案 id")
