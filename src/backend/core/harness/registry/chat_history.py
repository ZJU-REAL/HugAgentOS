"""Composition contract for user-scoped history tools."""
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

@dataclass
class HistoryRegistration:
    factory: Callable | None = None

@lru_cache(maxsize=1)
def history_registration():
    return HistoryRegistration()

def register_chat_history(factory: Callable):
    history_registration().factory = factory

def history_operations(**scope):
    factory = history_registration().factory
    if factory is None:
        raise RuntimeError("Chat history services are not registered")
    return factory(**scope)
