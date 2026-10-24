"""Permission declarations, execution tickets and final effect guards."""

from __future__ import annotations
import hashlib
import json
import os
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence
from core.sandbox.permissions.approval import APPROVAL_ASK, APPROVAL_FULL

if TYPE_CHECKING:
    from core.sandbox.permissions.os_gate import LocalAccessDecision
    from core.sandbox.oslayer import SandboxLaunch
    from core.projects.edition import ProjectScope

DOMAIN_LOCAL_PATH = "local_path"
DOMAIN_LOCAL_COMMAND = "local_command"
DOMAIN_MYSPACE = "myspace"
DOMAIN_APPROVAL = "approval"
DOMAIN_DENY = "deny"
READ = "read"
WRITE = "write"
EXECUTE = "execute"


def _json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _call_args(tool_call: Any) -> dict[str, Any]:
    raw = getattr(tool_call, "input", "") or "{}"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        parsed = {"_raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


def _canonical_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


@dataclass(frozen=True)
class PermissionRuntime:
    """Run-scoped context needed by policy and approval routing.

    ``interactive`` preserves the existing My Space pre-authorization path for
    channel runs. ``approval_available`` is stricter: it is true only when a
    live UI can answer a new built-in-tool confirmation. ``default_allow`` is
    set for trusted unattended entry points (external channels and automation):
    governed built-ins bypass policy prompts but still receive a matching
    execution ticket for the final file/command boundary.

    ``approval_mode`` is the user's own preset for this run: ``auto`` answers
    an ordinary confirmation with "yes" instead of suspending the tool but
    still asks about destructive/dangerous ones, and ``full`` never asks.
    """

    chat_id: Optional[str]
    user_id: Optional[str]
    interactive: bool
    approval_available: bool
    default_allow: bool = False
    approval_mode: str = APPROVAL_ASK
    account_approval: bool = False
    read_only: bool = False
    sandbox_session_id: Optional[str] = None
    project_scope: Optional[ProjectScope] = None


# What an intent does when no live UI can answer a confirmation (background runs,
# sub-agents, IM channel runs, scheduled automation). Declared per intent rather
# than assumed globally: refusing is right for host access the user never saw,
# but applying it to governance that previously passed through silently removes
# working capability from every headless run.
FALLBACK_DENY = "deny"
FALLBACK_ALLOW = "allow"


@dataclass(frozen=True)
class PermissionIntent:
    domain: str
    action: str
    target: str
    summary: str
    op: str = ""
    kind: str = ""
    on_no_ui: str = FALLBACK_DENY

    def audit_dict(self) -> dict[str, str]:
        return {
            "domain": self.domain,
            "action": self.action,
            "target": self.target,
            "op": self.op,
            "kind": self.kind,
            "on_no_ui": self.on_no_ui,
        }


IntentResolver = Callable[[Mapping[str, Any], PermissionRuntime], Sequence[PermissionIntent]]


@dataclass(frozen=True)
class ToolPermissionSpec:
    """One developer-owned permission declaration for a visible tool name."""

    key: str
    resolver: IntentResolver

    def with_resolver(self, resolver: IntentResolver) -> ToolPermissionSpec:
        return replace(self, resolver=resolver)


# 本机命令的隔离范围完全由用户自己选的权限档决定，只有两种结果：
#   ``full``     —— 用户明确选了「以我的身份直接跑」，本次不加任何 OS 约束；
#   其余任何档   —— 一律进 OS 沙箱。宿主装不了后端、或这条策略在这个平台上执行不了，
#                   就把命令拒掉并说清原因，不存在「退化成只靠命令字符串把关」这条路。
UNCONFINED_APPROVAL_MODES = frozenset({APPROVAL_FULL})

# 本机安全配置读不出来时用的记号档：不属于任何用户可选档位，落到最严格的一侧。
FAIL_CLOSED_MODE = "fail_closed"


class LocalConfinementUnavailableError(Exception):
    """The chosen preset requires OS confinement that this host cannot provide."""


@dataclass(frozen=True)
class LocalCommandAuthorization:
    """One command's pre-execution verdict together with its sandbox scope.

    The permission layer decides *what* the command may touch; the execution
    boundary only asks this object to turn that into a confined launch. Neither
    side re-derives preset semantics or platform support for itself.
    """

    command: str
    approval_mode: str
    access: "LocalAccessDecision"
    workspace_root: str
    cwd: Optional[str] = None

    @property
    def confined(self) -> bool:
        return not self.access.unconfined

    def confine(self) -> Optional["SandboxLaunch"]:
        """The confined launch for this command.

        Returns ``None`` only when the user's preset asked for no confinement.
        Anything else either returns a real launch or raises
        :class:`LocalConfinementUnavailableError` explaining which permission
        choice would let the command run.
        """
        import core.sandbox.permissions.os_gate as os_sandbox
        from core.sandbox.oslayer import (
            SandboxUnavailableError,
            SandboxUnenforceableError,
        )

        if not self.confined:
            return None
        policy = os_sandbox.build_policy(self.access)
        context = os_sandbox.build_context(workspace_root=self.workspace_root, cwd=self.cwd)
        try:
            return os_sandbox.confine(policy, context)
        except (SandboxUnavailableError, SandboxUnenforceableError) as exc:
            raise LocalConfinementUnavailableError(
                f"{exc}；当前「{self.approval_mode}」权限档要求由操作系统强制隔离，"
                "已拒绝执行。可在输入框上方切换权限档，或在「设置 → 本地权限」"
                "调整授权范围后重试。"
            ) from exc


class TicketLifetime:
    """票据的有效期标记，由中间件在分派前后翻转。

    ``CURRENT_PERMISSION_TICKET`` 是 ContextVar：工具体内 ``create_task`` 派生的子任务
    会**复制**当前上下文，中间件退出时的 ``reset`` 管不到那份副本 —— 票据会随子任务
    一直活着。把有效期放在票据对象自己身上（而不是只靠 ContextVar 的生命周期），
    子任务拿到的是同一个对象，主分派一结束它那份也立刻失效。
    """

    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True

    def expire(self) -> None:
        self.active = False


@dataclass(frozen=True)
class PermissionTicket:
    tool_name: str
    tool_call_id: str
    args_hash: str
    spec_key: str
    intents: tuple[PermissionIntent, ...]
    local_command: Optional[LocalCommandAuthorization] = None
    reasons: tuple[str, ...] = ()
    lifetime: TicketLifetime = field(default_factory=TicketLifetime)

    @property
    def active(self) -> bool:
        return self.lifetime.active

    def matches(self, tool_call: Any) -> bool:
        return (
            self.tool_name == str(getattr(tool_call, "name", "") or "")
            and self.tool_call_id == str(getattr(tool_call, "id", "") or "")
            and self.args_hash == _json_hash(_call_args(tool_call))
        )

    def authorizes_path(self, path: str, action: str) -> bool:
        wanted = _canonical_path(path)
        for intent in self.intents:
            if intent.domain != DOMAIN_LOCAL_PATH:
                continue
            if _canonical_path(intent.target) != wanted:
                continue
            if intent.action == action or (intent.action == WRITE and action == READ):
                return True
        return False

    def audit_dict(self) -> dict[str, Any]:
        return {
            "decision": "allow",
            "tool": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "spec_key": self.spec_key,
            "intents": [intent.audit_dict() for intent in self.intents],
            "reasons": list(self.reasons),
            "local_command": (
                {
                    "approval_mode": self.local_command.approval_mode,
                    "confined": self.local_command.confined,
                    "write_paths": list(self.local_command.access.writable_roots),
                    "network_allowed": self.local_command.access.network_allowed,
                }
                if self.local_command is not None
                else None
            ),
        }


@dataclass(frozen=True)
class PermissionOutcome:
    proceed: bool
    ticket: Optional[PermissionTicket] = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    audit: Mapping[str, Any] = field(default_factory=dict)


CURRENT_PERMISSION_TICKET: ContextVar[Optional[PermissionTicket]] = ContextVar(
    "jx_current_permission_ticket", default=None
)


class PermissionEnforcementError(Exception):
    """Raised when execution does not carry the matching pre-dispatch ticket.

    Deliberately **not** an ``OSError`` subclass: the file tools wrap their real
    I/O in ``except OSError``, and a ``PermissionError`` base would let a denial
    be reported to the user as an ordinary "read failed" disk error.
    """


def require_local_path_permission(path: str, action: str) -> None:
    """Second-line guard used immediately before direct host file I/O."""
    from core.config.local_mode import local_mode_enabled

    if not local_mode_enabled():
        return
    ticket = CURRENT_PERMISSION_TICKET.get()
    if ticket is None or not ticket.active or not ticket.authorizes_path(path, action):
        raise PermissionEnforcementError(
            f"本机文件{action}缺少匹配的预执行授权票据，已拒绝访问：{path}"
        )


def current_local_command_authorization(
    command: str,
) -> Optional[LocalCommandAuthorization]:
    ticket = CURRENT_PERMISSION_TICKET.get()
    if ticket is None or not ticket.active:
        return None
    auth = ticket.local_command
    return auth if auth is not None and auth.command == command else None
