"""local user types responsibilities."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, Optional

@dataclass
class RegisterResult:
    ok: bool
    message: str
    user_id: Optional[str] = None
    user_info: Optional[Dict[str, Any]] = None


@dataclass
class LoginResult:
    ok: bool
    message: str
    user_id: Optional[str] = None
    user_info: Optional[Dict[str, Any]] = None
