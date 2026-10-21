"""Validated commands and bounded binary event framing."""
import json
import struct
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

VIEWPORT_LIMITS = {"min_width": 160, "max_width": 3840, "min_height": 120, "max_height": 2880}

class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    actor: Literal["agent", "user"]
    connection_id: str = Field(default="", max_length=128)
    epoch: int | None = None
    action: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)

def packet(event: dict, data: bytes = b"") -> bytes:
    header = json.dumps({**event, "bytes": len(data)}, ensure_ascii=False).encode()
    return struct.pack(">I", len(header)) + header + data
