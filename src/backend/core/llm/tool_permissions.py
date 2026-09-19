"""Declarative permission gateway for explicitly governed agent tools.

The module separates four concerns that used to be embedded in individual
tools:

* a registry identifies the tools whose effects need platform governance;
* resolvers turn one tool call into concrete resource intents;
* the service evaluates policy and routes human approval;
* a short-lived ticket is consumed again at the real execution boundary.

Tools absent from the registry retain their existing execution behavior.  For
registered tools, AgentScope's own permission engine remains the coarse
framework admission layer while this gateway owns HugAgentOS's resource-level
decisions for built-in tools (local host paths/commands and My Space writes).
MCP tools are temporarily trusted and stay outside this registry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

from agentscope.agent import Agent
from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolResponse

if TYPE_CHECKING:  # imported lazily at runtime to keep this module dependency-light
    from core.sandbox.os_sandbox import LocalAccessDecision
    from core.sandbox.oslayer import SandboxLaunch

logger = logging.getLogger(__name__)

DOMAIN_LOCAL_PATH = "local_path"
DOMAIN_LOCAL_COMMAND = "local_command"
DOMAIN_MYSPACE = "myspace"
DOMAIN_APPROVAL = "approval"
DOMAIN_DENY = "deny"

READ = "read"
WRITE = "write"
EXECUTE = "execute"

# 权限档：用户在输入框工具栏自己选的一档，决定"要不要为工具调用停下来问他"。
# ask  —— 逐项确认，保持原有行为。
# auto —— 替我批准：普通写入直接过，删除等被判定为危险的操作仍然问一句。
# full —— 完全放开：一律不问。
APPROVAL_ASK = "ask"
APPROVAL_AUTO = "auto"
APPROVAL_FULL = "full"
APPROVAL_MODES = (APPROVAL_ASK, APPROVAL_AUTO, APPROVAL_FULL)

# 早期版本用过的名字，读到时按最保守的一档兜底，不让老值静默变成放行。
_LEGACY_APPROVAL_ALIASES = {"standard": APPROVAL_ASK, "readonly": APPROVAL_ASK}

# 「替我批准」档下仍要停下来问的操作：删掉的东西找不回来，值得多按一次。
DESTRUCTIVE_OPS = frozenset({"delete", "cron_delete"})


def normalize_approval_mode(raw: Any) -> str:
    """Coerce a stored or user-supplied preset name onto the known vocabulary."""
    mode = str(raw or "").strip().lower()
    if mode in APPROVAL_MODES:
        return mode
    return _LEGACY_APPROVAL_ALIASES.get(mode, APPROVAL_ASK)


def resolve_approval_mode(explicit: Any, *, user_id: Optional[str]) -> str:
    """本次运行的权限档：调用方显式给了就用它，否则回落到用户自己存的那一档。

    权限档是**每个用户一份**的设置，任何一个新起 agent 的入口（子智能体、
    计划步骤、批量执行）都该拿到同一份。靠每个调用点各自透传，漏一个就等于
    悄悄退回「逐项确认」——用户明明选了「完全放开」，换条路径照样被问。
    """
    if explicit is not None:
        return normalize_approval_mode(explicit)
    if not user_id:
        return APPROVAL_ASK
    try:
        from core.db.engine import SessionLocal
        from core.services.user_service import UserService

        with SessionLocal() as db:
            stored = UserService(db).get_user_settings(str(user_id)).get("tool_approval_mode")
    except Exception:  # noqa: BLE001 - 读不到就按最保守的一档
        logger.warning("[tool-permission] 权限档读取失败，本次按逐项确认处理", exc_info=True)
        return APPROVAL_ASK
    return normalize_approval_mode(stored)


def _preset_answers(mode: str, *, dangerous: bool) -> bool:
    if mode == APPROVAL_FULL:
        return True
    return mode == APPROVAL_AUTO and not dangerous


def preset_answers_for_user(
    user_id: Optional[str], *, op: str = "", dangerous: bool = False
) -> bool:
    """执行期复查：这个用户的权限档是不是已经替他答了这次确认。

    留给在 dispatch 之外**自己发起**确认的地方（「我的空间」的登记器就是这一类）：
    它们不经过 ``on_acting``，拿不到 ``PermissionRuntime``，但判定必须和这里同源。

    按 ``user_id`` 取档而不是读异步上下文：登记器跑在自己的任务里，任何 ContextVar
    在那里读到的都是默认值 —— 用户明明选了「完全放开」，还会逐个文件弹确认。档位的
    真源本来就是用户设置。
    """
    return _preset_answers(
        resolve_approval_mode(None, user_id=user_id),
        dangerous=dangerous or op in DESTRUCTIVE_OPS,
    )


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
    sandbox_session_id: Optional[str] = None


# What an intent does when no live UI can answer a confirmation (batch runs,
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
        from core.sandbox import os_sandbox
        from core.sandbox.oslayer import (
            SandboxUnavailableError,
            SandboxUnenforceableError,
        )

        if not self.confined:
            return None
        policy = os_sandbox.build_policy(self.access)
        context = os_sandbox.build_context(workspace_root=self.workspace_root)
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


class ToolPermissionRegistry:
    """Mutable run-scoped governed-tool list; plugins extend the same object."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolPermissionSpec] = {}
        self._sources: dict[str, str] = {}

    def register(
        self,
        tool_name: str,
        spec: ToolPermissionSpec,
        *,
        source: str,
    ) -> None:
        name = str(tool_name or "").strip()
        if not name:
            raise ValueError("tool permission name must not be empty")
        previous = self._specs.get(name)
        if previous is not None and previous.key != spec.key:
            raise ValueError(
                "conflicting permission declarations for tool "
                f"{name!r}: {previous.key!r} from {self._sources[name]!r} "
                f"vs {spec.key!r} from {source!r}"
            )
        self._specs[name] = spec
        self._sources[name] = source

    def get(self, tool_name: str) -> Optional[ToolPermissionSpec]:
        return self._specs.get(tool_name)

    def contains(self, tool_name: str) -> bool:
        return tool_name in self._specs

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)


@dataclass(frozen=True)
class McpPermissionScan:
    """Compatibility result for the temporarily disabled MCP policy scan."""

    names: frozenset[str]
    unresolved: tuple[Any, ...]


async def register_mcp_client_permissions(
    registry: ToolPermissionRegistry,
    clients: Sequence[Any],
    server_configs: Mapping[str, Mapping[str, Any]],
) -> McpPermissionScan:
    """Leave every MCP tool outside the permission registry.

    MCP is currently a trusted whitelist.  Do not enumerate clients here: an
    unavailable MCP server must follow the normal connection lifecycle rather
    than being withdrawn by a permission subsystem that does not govern it.
    The parameters and result type remain for compatibility with older callers.
    """
    del registry, clients, server_configs
    return McpPermissionScan(frozenset(), ())


def allow_tool() -> ToolPermissionSpec:
    return ToolPermissionSpec("allow", lambda _args, _runtime: ())


def deny_tool(reason: str) -> ToolPermissionSpec:
    def resolve(_args: Mapping[str, Any], _runtime: PermissionRuntime):
        return (
            PermissionIntent(
                domain=DOMAIN_DENY,
                action="deny",
                target="",
                summary=reason,
            ),
        )

    return ToolPermissionSpec(f"deny:{reason}", resolve)


def _is_myspace_target(logical: str, physical: str, user_id: Optional[str]) -> bool:
    """Whether this access lands in the user's persistent My Space area.

    Judged on the **physical** path, the same identity the tools themselves use
    to decide reverse-sync. Testing the raw argument for a ``/myspace`` prefix
    would miss the equally-supported physical spelling
    ``/workspace/myspace/<uid>/…``, letting a caller reach persistent storage
    with no confirmation at all.
    """
    if logical == "/myspace" or logical.startswith("/myspace/"):
        return True
    from core.llm.tools._paths import is_myspace_physical

    return bool(is_myspace_physical(physical, user_id))


def _path_summary(tool: str, action: str, logical: str, args: Mapping[str, Any]) -> str:
    if tool == "Write":
        return f"写入 {logical}（{len(str(args.get('content') or ''))} 字符）"
    if tool == "Edit":
        return f"编辑 {logical}（替换片段）"
    if tool == "Move":
        return f"移动/改名 {logical} → {str(args.get('dst_path') or '')}"
    labels = {
        "Read": "读取",
        "Glob": "扫描",
        "Grep": "搜索",
        "read_image": "查看图片",
        "Delete": "删除",
        "CreateFolder": "创建文件夹",
        "sandbox_put_artifact": "写入沙盒文件",
        "sandbox_get_artifact": "读取沙盒文件",
    }
    return f"{labels.get(tool, action)} {logical}"


def local_path_tool(
    path_arg: str,
    action: str,
    *,
    tool_name: str = "",
    additional_paths: Sequence[tuple[str, str]] = (),
    myspace_op: str = "",
    skip_if_arg: str = "",
    default_path: str = "",
) -> ToolPermissionSpec:
    """Declare a tool whose effects are host paths taken from its arguments.

    ``default_path`` mirrors the tool signature's own default, so a call that
    omits the optional argument is still governed instead of resolving to zero
    intents and passing through ungoverned.
    """
    path_specs = ((path_arg, action), *tuple(additional_paths))
    key = "path:" + ",".join(f"{arg}:{mode}" for arg, mode in path_specs)
    if myspace_op:
        key += f":myspace:{myspace_op}"
    if default_path:
        key += f":default:{default_path}"

    def resolve(args: Mapping[str, Any], runtime: PermissionRuntime):
        if skip_if_arg and str(args.get(skip_if_arg) or "").strip():
            return ()
        intents: list[PermissionIntent] = []
        first: Optional[tuple[str, str]] = None
        from core.llm.tools._paths import to_physical_path

        for index, (arg, mode) in enumerate(path_specs):
            logical = str(args.get(arg) or "").strip()
            if not logical and index == 0:
                logical = default_path
            if not logical:
                continue
            session = runtime.sandbox_session_id or runtime.chat_id
            physical = to_physical_path(logical, runtime.user_id, session_id=session)
            if first is None:
                first = (logical, physical)
            intents.append(
                PermissionIntent(
                    domain=DOMAIN_LOCAL_PATH,
                    action=mode,
                    target=physical,
                    summary=_path_summary(tool_name, mode, logical, args),
                )
            )
        if myspace_op and first is not None and _is_myspace_target(*first, runtime.user_id):
            intents.append(
                PermissionIntent(
                    domain=DOMAIN_MYSPACE,
                    action=WRITE,
                    target=first[0],
                    summary=_path_summary(tool_name, WRITE, first[0], args),
                    op=myspace_op,
                    kind="myspace",
                )
            )
        return tuple(intents)

    return ToolPermissionSpec(key, resolve)


def local_command_tool() -> ToolPermissionSpec:
    def resolve(args: Mapping[str, Any], _runtime: PermissionRuntime):
        command = str(args.get("command") or "").strip()
        if not command:
            return ()
        return (
            PermissionIntent(
                domain=DOMAIN_LOCAL_COMMAND,
                action=EXECUTE,
                target=command,
                summary=f"在本机执行：{command[:200]}",
                op="local_exec",
                kind="local_cmd",
            ),
        )

    return ToolPermissionSpec("local-command:command", resolve)


def mcp_tool_permission(
    server_name: str,
    tool_name: str,
    server_config: Mapping[str, Any],
) -> Optional[ToolPermissionSpec]:
    """Keep MCP outside the built-in-tool permission gateway.

    MCP servers are currently a trusted whitelist, so even persisted
    ``confirm``/``deny`` configuration is intentionally ignored.
    """
    del server_name, tool_name, server_config
    return None


def builtin_tool_permission(tool_name: str) -> Optional[ToolPermissionSpec]:
    """Return the rule for a governed native tool, otherwise pass through."""
    from core.llm.tools._myspace_confirm import OP_DELETE, OP_EDIT, OP_MKDIR, OP_MOVE, OP_WRITE

    governed: dict[str, ToolPermissionSpec] = {
        "Read": local_path_tool("file_path", READ, tool_name="Read"),
        "Write": local_path_tool("file_path", WRITE, tool_name="Write", myspace_op=OP_WRITE),
        "Edit": local_path_tool("file_path", WRITE, tool_name="Edit", myspace_op=OP_EDIT),
        # ``path`` is optional on both; mirror their signature default so an
        # omitted argument is still governed rather than resolving to no intent.
        "Glob": local_path_tool("path", READ, tool_name="Glob", default_path="/workspace"),
        "Grep": local_path_tool("path", READ, tool_name="Grep", default_path="/workspace"),
        "read_image": local_path_tool(
            "file_path",
            READ,
            tool_name="read_image",
            skip_if_arg="file_id",
        ),
        "Delete": local_path_tool("path", WRITE, tool_name="Delete", myspace_op=OP_DELETE),
        "Move": local_path_tool(
            "src_path",
            WRITE,
            tool_name="Move",
            additional_paths=(("dst_path", WRITE),),
            myspace_op=OP_MOVE,
        ),
        "CreateFolder": local_path_tool(
            "path", WRITE, tool_name="CreateFolder", myspace_op=OP_MKDIR
        ),
        "bash": local_command_tool(),
        "Bash": local_command_tool(),
        "sandbox_put_artifact": local_path_tool(
            "dest_path", WRITE, tool_name="sandbox_put_artifact"
        ),
        "sandbox_get_artifact": local_path_tool("src_path", READ, tool_name="sandbox_get_artifact"),
        # 批量作业会把 ``script_path`` 的内容读出来当脚本正文执行，``dest_path`` 是
        # 它导出台账的落点。两者都是模型给的裸路径，必须和其它文件工具同样受管，
        # 否则这条路等于一个不受策略约束的任意路径读取通道。
        "run_job": local_path_tool(
            "script_path",
            READ,
            tool_name="run_job",
            additional_paths=(("dest_path", WRITE),),
        ),
    }
    return governed.get(tool_name)


def _too_broad_write_root(path: str) -> bool:
    """Whether granting ``path`` as a writable root would hand out far too much.

    A not-yet-created target has to be widened to its parent directory for the
    file to be creatable at all. That is fine for ``~/project/out.txt`` and very
    much not fine for ``~/out.txt``: the home directory holds every credential
    file and dotfile the user owns. Compare by resolved identity so a symlinked
    home is recognised too.
    """
    resolved = _canonical_path(path)
    if resolved == os.path.dirname(resolved):  # filesystem / drive root
        return True
    try:
        home = _canonical_path(os.path.expanduser("~"))
    except (OSError, RuntimeError):
        return False
    return resolved == home


def _one_shot_write_root(path: str) -> Optional[str]:
    target = _canonical_path(path)
    if os.path.exists(target):
        return target
    parent = os.path.dirname(target)
    if parent == target or not os.path.isdir(parent):
        return None
    if _too_broad_write_root(parent):
        logger.info("[tool-permission] refusing over-broad one-shot write root %r", parent)
        return None
    return parent


class ToolPermissionService:
    """Policy decision point for explicitly governed built-in tools."""

    def __init__(
        self,
        registry: ToolPermissionRegistry,
        runtime: PermissionRuntime,
    ) -> None:
        self.registry = registry
        self.runtime = runtime

    @staticmethod
    def _blocked(error: str, **extra: Any) -> dict[str, Any]:
        return {"error": error, "blocked": True, **extra}

    async def authorize(self, tool_call: Any) -> PermissionOutcome:
        name = str(getattr(tool_call, "name", "") or "")
        call_id = str(getattr(tool_call, "id", "") or "")
        args = _call_args(tool_call)
        spec = self.registry.get(name)
        if spec is None:
            return PermissionOutcome(
                True,
                audit={
                    "decision": "allow_unregistered",
                    "tool": name,
                    "tool_call_id": call_id,
                },
            )
        try:
            intents = tuple(spec.resolver(args, self.runtime))
        except Exception as exc:  # noqa: BLE001 - declaration failures fail closed
            logger.exception("[tool-permission] resolver failed tool=%s", name)
            resolver_payload = self._blocked(f"权限声明解析失败，工具已拒绝执行：{exc}")
            return PermissionOutcome(
                False,
                payload=resolver_payload,
                audit={
                    "decision": "deny",
                    "tool": name,
                    "reason": "resolver_failed",
                },
            )

        reasons: list[str] = []
        if self.runtime.default_allow:
            reasons.append("trusted_unattended_run")
        local_command: Optional[LocalCommandAuthorization] = None
        for intent in intents:
            result = await self._authorize_intent(intent)
            reasons.extend(result.get("reasons") or [])
            if result.get("local_command") is not None:
                local_command = result["local_command"]
            intent_payload = result.get("payload")
            if intent_payload is not None:
                decision = "allow_deduplicated" if intent_payload.get("ok") else "deny"
                return PermissionOutcome(
                    False,
                    payload=intent_payload,
                    audit={
                        "decision": decision,
                        "tool": name,
                        "tool_call_id": call_id,
                        "intent": intent.audit_dict(),
                        "reasons": list(result.get("reasons") or []),
                    },
                )

        ticket = PermissionTicket(
            tool_name=name,
            tool_call_id=call_id,
            args_hash=_json_hash(args),
            spec_key=spec.key,
            intents=intents,
            local_command=local_command,
            reasons=tuple(dict.fromkeys(reasons)),
        )
        audit = ticket.audit_dict()
        if self.runtime.default_allow:
            audit["decision"] = "allow_trusted_unattended"
        logger.info(
            "[tool-permission] decision=%s tool=%s call=%s intents=%s reasons=%s",
            audit["decision"],
            name,
            call_id,
            [intent.domain for intent in intents],
            ticket.reasons,
        )
        return PermissionOutcome(True, ticket=ticket, audit=audit)

    def _answers_for_user(self, *, dangerous: bool) -> bool:
        """Whether a would-be confirmation is answered "yes" without asking.

        「完全放开」一律不问；「替我批准」只替用户过普通操作，删除和被本地
        安全策略判为危险的那些仍旧停下来问。任何一档都只跳过"问"这一步——
        策略判定的硬拒绝照样拒绝。

        受信任的无人值守入口（定时任务、IM 渠道）同样只跳过"问"：没有人在场可答
        的确认视为已答应，这样这些入口的既有能力一条不少；但它们**不再**绕过策略
        判定本身 —— ``deny`` 依旧拒绝，沙箱范围依旧按用户配置收拢。
        """
        if self.runtime.default_allow:
            return True
        return _preset_answers(self.runtime.approval_mode, dangerous=dangerous)

    def _no_ui_pass_through(self, intent: PermissionIntent) -> bool:
        """Whether this intent is declared to pass through when nobody can answer.

        The fallback is a field on the intent, not a rule any single domain
        invents for itself: refusing is right for host access the user never
        saw, and passing through is right for governance that always used to
        happen silently. Both answers live in the declaration.
        """
        if self.runtime.approval_available and self.runtime.chat_id:
            return False
        return intent.on_no_ui == FALLBACK_ALLOW

    async def _authorize_intent(self, intent: PermissionIntent) -> dict[str, Any]:
        if intent.domain == DOMAIN_DENY:
            return {"payload": self._blocked(intent.summary), "reasons": [intent.summary]}
        if intent.domain == DOMAIN_LOCAL_PATH:
            return await self._authorize_local_path(intent)
        if intent.domain == DOMAIN_LOCAL_COMMAND:
            return await self._authorize_local_command(intent)
        if intent.domain == DOMAIN_MYSPACE:
            return await self._authorize_approval(intent, interactive=self.runtime.interactive)
        if intent.domain == DOMAIN_APPROVAL:
            return await self._authorize_approval(
                intent, interactive=self.runtime.approval_available
            )
        return {
            "payload": self._blocked(f"未知权限域 {intent.domain!r}，工具已拒绝执行"),
            "reasons": ["unknown_permission_domain"],
        }

    def _safe_local_security(self):
        """Grants + effective local policy for this run's own permission preset.

        桌面端不再另存一份权限档：粗档就是 ``runtime.approval_mode``，本机策略
        由它翻译而来，授权目录与分类处置仍来自本机存储。
        """
        from core.sandbox.local_policy import DELETE, NETWORK, PRIVILEGE, SYSTEM_WRITE, Policy

        mode = self.runtime.approval_mode
        try:
            from core.services.local_grant_service import grants_for_gate, policy_for_gate

            return mode, grants_for_gate(), policy_for_gate(mode)
        except Exception:  # unreadable configuration must never grant access
            logger.exception("[tool-permission] local security config unreadable; failing closed")
            return (
                FAIL_CLOSED_MODE,
                [],
                Policy(
                    out_of_scope="block",
                    workspace_write="block",
                    danger={
                        DELETE: "block",
                        SYSTEM_WRITE: "block",
                        NETWORK: "block",
                        PRIVILEGE: "block",
                    },
                ),
            )

    def _session_workspace(self) -> str:
        """本次对话的工作目录——权限闸里的"工作区"就是它。

        目录内自由读写；出了这个目录的绝对路径按授权与策略判定（放行 / 需确认 /
        拦截），不额外加规则。
        """
        from core.sandbox._common import WORKSPACE
        from services.script_runner_service.workspace_paths import session_root

        session = self.runtime.sandbox_session_id or self.runtime.chat_id
        return session_root(WORKSPACE, str(session)) if session else WORKSPACE

    async def _authorize_local_path(self, intent: PermissionIntent) -> dict[str, Any]:
        from core.config.local_mode import local_mode_enabled

        if not local_mode_enabled():
            return {}
        from core.sandbox.local_policy import danger_categories, evaluate_local_path

        _mode, grants, policy = self._safe_local_security()
        verdict = evaluate_local_path(
            intent.target,
            intent=intent.action,
            grants=grants,
            policy=policy,
            workspace_root=self._session_workspace(),
            platform="windows" if os.name == "nt" else "posix",
        )
        logger.info(
            "[tool-permission] local-path decision=%s action=%s path=%r reasons=%s",
            verdict.decision,
            intent.action,
            intent.target,
            verdict.reasons,
        )
        if verdict.decision == "deny":
            return {
                "payload": self._blocked(
                    "该本机文件操作被安全策略拦截（"
                    + "、".join(verdict.reasons)
                    + "）。如确需访问，请在「设置 → 本地权限」调整授权后重试。"
                ),
                "reasons": verdict.reasons,
            }
        if verdict.decision != "confirm":
            return {"reasons": verdict.reasons}
        if self._answers_for_user(dangerous=bool(danger_categories(verdict.reasons))):
            return {
                "reasons": [*verdict.reasons, f"approved_by_preset:{self.runtime.approval_mode}"]
            }

        if self._no_ui_pass_through(intent):
            return {"reasons": [*verdict.reasons, "no_approval_ui_pass_through:local_path"]}

        from core.llm.tools import _myspace_confirm as confirm

        op = confirm.OP_LOCAL_READ if intent.action == READ else confirm.OP_LOCAL_WRITE
        blocked = await confirm.gate(
            chat_id=self.runtime.chat_id,
            op=op,
            logical_path=intent.target,
            interactive=bool(self.runtime.approval_available and self.runtime.chat_id),
            summary=intent.summary,
            kind=f"{confirm.KIND_LOCAL_PATH_PREFIX}{intent.action}",
        )
        return (
            {"payload": blocked, "reasons": verdict.reasons}
            if blocked
            else {"reasons": verdict.reasons}
        )

    async def _authorize_local_command(self, intent: PermissionIntent) -> dict[str, Any]:
        from core.config.local_mode import local_mode_enabled

        if not local_mode_enabled():
            return {}
        from core.sandbox._common import WORKSPACE
        from core.sandbox.local_policy import (
            Grant,
            danger_categories,
            evaluate_local_command,
        )

        platform = "windows" if os.name == "nt" else "posix"
        approval_mode, grants, policy = self._safe_local_security()
        eval_grants = list(grants)
        if WORKSPACE != "/workspace":
            eval_grants.append(Grant(WORKSPACE, "readwrite"))
        verdict = evaluate_local_command(
            intent.target,
            cwd="/workspace",
            grants=eval_grants,
            policy=policy,
            workspace_root="/workspace",
            platform=platform,
        )
        if verdict.decision == "deny":
            return {
                "payload": self._blocked(
                    "该命令被本地安全策略拦截（"
                    + "、".join(verdict.reasons)
                    + "）。如确需执行，请在「设置 → 本地权限」调整策略后重试。",
                    exit_code=-1,
                ),
                "reasons": verdict.reasons,
            }
        if (
            verdict.decision == "confirm"
            and not self._answers_for_user(dangerous=bool(danger_categories(verdict.reasons)))
            and not self._no_ui_pass_through(intent)
        ):
            from core.llm.tools import _myspace_confirm as confirm

            blocked = await confirm.gate(
                chat_id=self.runtime.chat_id,
                op=confirm.OP_LOCAL_EXEC,
                logical_path=intent.target[:160],
                interactive=bool(self.runtime.approval_available and self.runtime.chat_id),
                summary=intent.summary
                + (f"（{'、'.join(verdict.reasons)}）" if verdict.reasons else ""),
                kind=confirm.KIND_LOCAL_CMD,
            )
            if blocked is not None:
                return {"payload": blocked, "reasons": verdict.reasons}

        return {
            "reasons": verdict.reasons,
            "local_command": self._local_command_authorization(
                command=intent.target,
                approval_mode=approval_mode,
                grants=grants,
                policy=policy,
                one_shot_write_targets=tuple(verdict.write_paths),
            ),
        }

    def _physical(self, logical: str) -> str:
        """Host path for a model-facing path.

        The command classifier works in the model's ``/workspace`` namespace, so
        the write targets it reports are logical. Handing those straight to the
        sandbox would name a directory that does not exist on this host and the
        approved one-shot write would be silently dropped.
        """
        try:
            from core.llm.tools._paths import to_physical_path

            session = self.runtime.sandbox_session_id or self.runtime.chat_id
            return to_physical_path(logical, self.runtime.user_id, session_id=session)
        except Exception:  # noqa: BLE001 - an untranslatable path stays as given
            return logical

    def _local_command_authorization(
        self,
        *,
        command: str,
        approval_mode: str,
        grants: Sequence[Any],
        policy: Any,
        one_shot_write_targets: Sequence[str] = (),
    ) -> LocalCommandAuthorization:
        """Turn this run's permission configuration into a sandbox scope.

        Every local command goes through here, prompted or not, so an unattended
        entry point is confined exactly like an interactive one — it only skips
        the asking, never the sandbox.
        """
        from core.sandbox._common import WORKSPACE
        from core.sandbox.local_policy import (
            NETWORK,
            SYSTEM_WRITE,
            intersects_system_write_area,
        )
        from core.sandbox.os_sandbox import LocalAccessDecision

        platform = "windows" if os.name == "nt" else "posix"
        may_write = policy.workspace_write != "block"
        system_write_allowed = policy.disposition_for(SYSTEM_WRITE) == "allow"

        writable: list[str] = [WORKSPACE] if may_write else []

        def _admit(candidate: str) -> None:
            if not candidate or candidate in writable:
                return
            # A protected system area never becomes writable implicitly — not
            # via a standing grant and not via a one-shot target either. The
            # same rule has to cover both, otherwise widening a not-yet-created
            # target to its parent directory can hand out /etc.
            if not system_write_allowed and intersects_system_write_area(candidate, platform):
                logger.info("[tool-permission] refusing system-area writable root %r", candidate)
                return
            writable.append(candidate)

        readable: list[str] = []
        for grant in grants:
            if grant.mode == "readwrite" and may_write:
                _admit(grant.path)
            else:
                readable.append(grant.path)
        if may_write:
            for target in one_shot_write_targets:
                root = _one_shot_write_root(self._physical(target))
                if root:
                    _admit(root)

        from core.sandbox.os_sandbox import protected_read_paths

        return LocalCommandAuthorization(
            command=command,
            approval_mode=approval_mode,
            workspace_root=self._session_workspace(),
            access=LocalAccessDecision(
                approval_mode=approval_mode,
                unconfined=approval_mode in UNCONFINED_APPROVAL_MODES,
                writable_roots=tuple(writable),
                readable_roots=tuple(dict.fromkeys(readable)),
                # This install's own directories stay unreadable whatever the
                # preset: they hold the local database, the desktop bridge
                # secret and the capability store, and a command that could read
                # them could impersonate the signed-in user against the local
                # backend. The workspace sits inside one of them and remains
                # reachable — the sandbox resolves the narrowest entry, and the
                # workspace root admitted above is deeper.
                denied_paths=protected_read_paths(),
                # A read-only preset gets no scratch space either; "may not
                # write" would be a strange thing to say while handing out a
                # writable directory.
                writable_scratch=may_write,
                # The command-level gate has already ruled on this specific
                # command's network use; only an explicit block turns the
                # sandbox's own network dimension on.
                network_allowed=policy.disposition_for(NETWORK) != "block",
            ),
        )

    async def _authorize_approval(
        self,
        intent: PermissionIntent,
        *,
        interactive: bool,
    ) -> dict[str, Any]:
        from core.llm.tools import _myspace_confirm as confirm

        op = intent.op or intent.action
        if self._answers_for_user(dangerous=op in DESTRUCTIVE_OPS):
            logger.info(
                "[tool-permission] preset=%s answered the confirmation; not asking op=%s target=%r",
                self.runtime.approval_mode,
                op,
                intent.target,
            )
            return {"reasons": [f"approved_by_preset:{self.runtime.approval_mode}"]}

        if not interactive and intent.on_no_ui == FALLBACK_ALLOW:
            logger.info(
                "[tool-permission] no approval UI; declared pass-through op=%s target=%r",
                intent.op or intent.action,
                intent.target,
            )
            return {"reasons": [f"no_approval_ui_pass_through:{intent.op or intent.action}"]}

        blocked = await confirm.gate(
            chat_id=self.runtime.chat_id,
            op=intent.op or intent.action,
            logical_path=intent.target,
            interactive=bool(interactive and self.runtime.chat_id),
            summary=intent.summary,
            kind=intent.kind or confirm.KIND_TOOL_PERMISSION,
        )
        return {"payload": blocked} if blocked is not None else {}


def _permission_response(
    tool_call: Any,
    payload: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> ToolResponse:
    success = bool(payload.get("ok")) and not payload.get("error")
    return ToolResponse(
        id=str(getattr(tool_call, "id", "") or ""),
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False, default=str),
            )
        ],
        state=ToolResultState.SUCCESS if success else ToolResultState.ERROR,
        metadata={"permission": dict(audit)},
    )


class ToolPermissionMiddleware(MiddlewareBase):
    """Authorize once before effects and bind the ticket around dispatch."""

    def __init__(self, service: ToolPermissionService) -> None:
        self.service = service

    async def on_acting(self, agent: Agent, input_kwargs: dict, next_handler):  # noqa: ANN001
        async for item in self._act(input_kwargs, next_handler):
            yield item

    async def _act(self, input_kwargs: dict, next_handler):  # noqa: ANN001
        tool_call = input_kwargs.get("tool_call")
        outcome = await self.service.authorize(tool_call)
        if not outcome.proceed:
            yield _permission_response(
                tool_call,
                outcome.payload or {"error": "工具权限判定拒绝执行", "blocked": True},
                outcome.audit,
            )
            return

        if outcome.ticket is None:
            async for item in next_handler(**input_kwargs):
                if isinstance(item, ToolResponse):
                    item.metadata = {
                        **dict(item.metadata or {}),
                        "permission": dict(outcome.audit),
                    }
                yield item
            return

        token = CURRENT_PERMISSION_TICKET.set(outcome.ticket)
        try:
            async for item in next_handler(**input_kwargs):
                if isinstance(item, ToolResponse):
                    item.metadata = {
                        **dict(item.metadata or {}),
                        "permission": outcome.ticket.audit_dict(),
                    }
                yield item
        finally:
            outcome.ticket.lifetime.expire()
            CURRENT_PERMISSION_TICKET.reset(token)


__all__ = [
    "CURRENT_PERMISSION_TICKET",
    "FAIL_CLOSED_MODE",
    "FALLBACK_ALLOW",
    "FALLBACK_DENY",
    "UNCONFINED_APPROVAL_MODES",
    "LocalCommandAuthorization",
    "LocalConfinementUnavailableError",
    "McpPermissionScan",
    "PermissionEnforcementError",
    "PermissionIntent",
    "PermissionOutcome",
    "PermissionRuntime",
    "PermissionTicket",
    "ToolPermissionMiddleware",
    "ToolPermissionRegistry",
    "ToolPermissionService",
    "ToolPermissionSpec",
    "allow_tool",
    "builtin_tool_permission",
    "current_local_command_authorization",
    "deny_tool",
    "local_command_tool",
    "local_path_tool",
    "mcp_tool_permission",
    "preset_answers_for_user",
    "register_mcp_client_permissions",
    "require_local_path_permission",
    "resolve_approval_mode",
]
