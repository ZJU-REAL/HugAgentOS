"""The runtime contract a chat mode hands to agent assembly."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple
from core.db.models.chat_mode import TOOL_SCOPE_ALL, TOOL_SCOPE_RESTRICTED

@dataclass(frozen=True)
class ChatModeSpec:
    """运行时装配契约——``agent_factory`` 只认这个，不认数据库行。"""

    slug: str
    name: str
    tool_scope: str = TOOL_SCOPE_ALL
    mcp_server_ids: Tuple[str, ...] = ()
    skill_ids: Tuple[str, ...] = ()
    plugin_ids: Tuple[str, ...] = ()
    agent_ids: Tuple[str, ...] = ()
    manual_invoke_enabled: bool = True
    #: 收窄模式下是否保留沙箱代码执行与文件工具；``all`` 作用域不看这个位。
    code_exec_enabled: bool = False
    max_iters: Optional[int] = None
    default_effort: str = "fast"
    effort_locked: bool = False
    prompt_kind: Optional[str] = None
    prompt_text: Optional[str] = None

    @property
    def restricted(self) -> bool:
        """是否收窄工具面。标准模式为 False（跟随 catalog 与用户授权）。"""
        return self.tool_scope == TOOL_SCOPE_RESTRICTED


SLUG_STANDARD = "standard"


SLUG_TURBO = "turbo"


BUILTIN_SLUGS = (SLUG_STANDARD, SLUG_TURBO)


STANDARD_SPEC = ChatModeSpec(
    slug=SLUG_STANDARD,
    name="标准模式",
    tool_scope=TOOL_SCOPE_ALL,
    code_exec_enabled=True,
    default_effort="fast",
)
