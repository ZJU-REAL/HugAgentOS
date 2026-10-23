"""Top-level chat execution supplied to durable chat workers by the host."""
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass
class ChatExecution:
    stream: Callable[..., AsyncIterator[Any]] | None = None


@lru_cache(maxsize=1)
def chat_execution() -> ChatExecution:
    return ChatExecution()


def register_chat_execution(stream: Callable[..., AsyncIterator[Any]]) -> None:
    chat_execution().stream = stream


def stream_chat_workflow(*, raw_user_message, session_messages, user_message, context):
    stream = chat_execution().stream
    if stream is None:
        raise RuntimeError("Chat execution workflow is not registered")
    return stream(raw_user_message=raw_user_message, session_messages=session_messages,
                  user_message=user_message, context=context)
