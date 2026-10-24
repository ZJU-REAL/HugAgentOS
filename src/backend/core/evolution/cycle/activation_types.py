"""activation types responsibilities."""
from __future__ import annotations


class ActivationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
