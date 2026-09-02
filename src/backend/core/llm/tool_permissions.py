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
from typing import Any, Callable, Mapping, Optional, Sequence

from agentscope.agent import Agent
from agentscope.message import TextBlock, ToolResultState
from agentscope.middleware import MiddlewareBase
from agentscope.tool._response import ToolResponse

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


CURRENT_APPROVAL_MODE: ContextVar[str] = ContextVar(
    "jx_current_approval_mode", default=APPROVAL_ASK
)


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


def preset_answers_confirmation(*, op: str = "", dangerous: bool = False) -> bool:
    """执行期复查：当前权限档是否已经替用户答了这次确认。

    留给工具在 dispatch 之后**自己发起**的确认（bash 把沙盒改动回写「我的
    空间」就是这一类）：它不经过 ``on_acting``，拿不到 ``PermissionRuntime``，
    但判定必须和这里同源，不能各写一份。
    """
    return _preset_answers(
        CURRENT_APPROVAL_MODE.get(), dangerous=dangerous or op in DESTRUCTIVE_OPS
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


CONFINE_NONE = "none"
CONFINE_PREFERRED = "preferred"
CONFINE_REQUIRED = "required"

# 本机 OS 沙箱约束契约，按权限档声明。``full`` 是唯一显式放弃约束的一档；
# ``ask`` / ``auto`` 优先约束，但在没有可用执行器的宿主上（今天的 Windows、
# 没装 bubblewrap 的 Linux）退化为只靠命令策略闸——在那里硬拒会让桌面客户端
# 彻底没有 shell 可用。表里没有的档位（例如配置读不出来的 fail-closed 记号）
# 一律按最严格的契约处理。
LOCAL_CONFINEMENT_BY_MODE: Mapping[str, str] = {
    APPROVAL_ASK: CONFINE_PREFERRED,
    APPROVAL_AUTO: CONFINE_PREFERRED,
    APPROVAL_FULL: CONFINE_NONE,
}

# 本机安全配置读不出来时用的记号档：不属于任何用户可选档位，落到最严格的契约。
FAIL_CLOSED_MODE = "fail_closed"


class LocalConfinementUnavailableError(Exception):
    """Raised when a preset that requires OS confinement cannot get it."""


@dataclass(frozen=True)
class ConfinedCommand:
    """Result of applying a preset's confinement contract to a command."""

    command: str
    confined: bool
    warning: str = ""


@dataclass(frozen=True)
class LocalCommandAuthorization:
    command: str
    approval_mode: str
    write_paths: tuple[str, ...] = ()

    @property
    def confinement(self) -> str:
        # Unknown presets are treated as the most restrictive contract.
        return LOCAL_CONFINEMENT_BY_MODE.get(self.approval_mode, CONFINE_REQUIRED)

    def confine(self, command: str) -> ConfinedCommand:
        """Apply this authorization's confinement contract to ``command``.

        The execution boundary calls exactly this; it never decides platform
        support or preset semantics for itself.
        """
        from core.sandbox.os_sandbox import (
            OsSandboxUnavailableError,
            confinement_unavailable_reason,
            wrap_command,
        )

        contract = self.confinement
        if contract == CONFINE_NONE:
            return ConfinedCommand(command, confined=False)

        reason = confinement_unavailable_reason()
        if not reason:
            try:
                return ConfinedCommand(
                    wrap_command(command, list(self.write_paths)),
                    confined=True,
                )
            except OsSandboxUnavailableError as exc:
                # The probe and the wrap are separate calls; the runner can go
                # away in between. Treat a late failure exactly like an
                # up-front one instead of escaping as an unhandled error.
                reason = str(exc)

        if contract == CONFINE_REQUIRED:
            raise LocalConfinementUnavailableError(
                f"{reason}；当前「{self.approval_mode}」权限档要求强制文件系统隔离，"
                "已拒绝执行。如确需在本机直接运行，请在输入框上方切换权限档。"
            )
        return ConfinedCommand(
            command,
            confined=False,
            warning=(
                f"{reason}；本次命令未受 OS 沙箱约束，仅由本地命令策略把关。"
                "如需强隔离请安装对应运行器。"
            ),
        )


@dataclass(frozen=True)
class PermissionTicket:
    tool_name: str
    tool_call_id: str
    args_hash: str
    spec_key: str
    intents: tuple[PermissionIntent, ...]
    local_command: Optional[LocalCommandAuthorization] = None
    reasons: tuple[str, ...] = ()

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
                    "write_paths": list(self.local_command.write_paths),
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
    if ticket is None or not ticket.authorizes_path(path, action):
        raise PermissionEnforcementError(
            f"本机文件{action}缺少匹配的预执行授权票据，已拒绝访问：{path}"
        )


def current_local_command_authorization(
    command: str,
) -> Optional[LocalCommandAuthorization]:
    ticket = CURRENT_PERMISSION_TICKET.get()
    auth = ticket.local_command if ticket is not None else None
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
        "view_image": "查看图片",
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
            physical = to_physical_path(logical, runtime.user_id)
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
        "view_image": local_path_tool(
            "file_path",
            READ,
            tool_name="view_image",
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
    }
    return governed.get(tool_name)


def _one_shot_write_root(path: str) -> Optional[str]:
    target = _canonical_path(path)
    if os.path.exists(target):
        return target
    parent = os.path.dirname(target)
    return parent if parent != target and os.path.isdir(parent) else None


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

        if self.runtime.default_allow:
            local_command = next(
                (
                    LocalCommandAuthorization(
                        command=intent.target,
                        approval_mode=APPROVAL_FULL,
                        write_paths=(),
                    )
                    for intent in intents
                    if intent.domain == DOMAIN_LOCAL_COMMAND
                ),
                None,
            )
            ticket = PermissionTicket(
                tool_name=name,
                tool_call_id=call_id,
                args_hash=_json_hash(args),
                spec_key=spec.key,
                intents=intents,
                local_command=local_command,
                reasons=("trusted_unattended_run",),
            )
            audit = ticket.audit_dict()
            audit["decision"] = "allow_trusted_unattended"
            logger.info(
                "[tool-permission] decision=allow_trusted_unattended " "tool=%s call=%s intents=%s",
                name,
                call_id,
                [intent.domain for intent in intents],
            )
            return PermissionOutcome(True, ticket=ticket, audit=audit)

        reasons: list[str] = []
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
        logger.info(
            "[tool-permission] decision=allow tool=%s call=%s intents=%s reasons=%s",
            name,
            call_id,
            [intent.domain for intent in intents],
            ticket.reasons,
        )
        return PermissionOutcome(True, ticket=ticket, audit=ticket.audit_dict())

    def _answers_for_user(self, *, dangerous: bool) -> bool:
        """Whether a would-be confirmation is answered "yes" without asking.

        「完全放开」一律不问；「替我批准」只替用户过普通操作，删除和被本地
        安全策略判为危险的那些仍旧停下来问。任何一档都只跳过"问"这一步——
        策略判定的硬拒绝照样拒绝。
        """
        return _preset_answers(self.runtime.approval_mode, dangerous=dangerous)

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

    async def _authorize_local_path(self, intent: PermissionIntent) -> dict[str, Any]:
        from core.config.local_mode import local_mode_enabled

        if not local_mode_enabled():
            return {}
        from core.sandbox._common import WORKSPACE
        from core.sandbox.local_policy import danger_categories, evaluate_local_path

        _mode, grants, policy = self._safe_local_security()
        verdict = evaluate_local_path(
            intent.target,
            intent=intent.action,
            grants=grants,
            policy=policy,
            workspace_root=WORKSPACE,
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
            SYSTEM_WRITE,
            Grant,
            danger_categories,
            evaluate_local_command,
            intersects_system_write_area,
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
        if verdict.decision == "confirm" and not self._answers_for_user(
            dangerous=bool(danger_categories(verdict.reasons))
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

        write_paths: list[str] = [WORKSPACE]
        system_write_allowed = policy.disposition_for(SYSTEM_WRITE) == "allow"

        def _admit(candidate: str) -> None:
            if not candidate or candidate in write_paths:
                return
            # A protected system area never becomes writable implicitly — not
            # via a standing grant and not via a one-shot target either. The
            # same rule has to cover both, otherwise widening a not-yet-created
            # target to its parent directory (below) can hand out /etc.
            if not system_write_allowed and intersects_system_write_area(candidate, platform):
                logger.info("[tool-permission] refusing system-area writable root %r", candidate)
                return
            write_paths.append(candidate)

        for grant in grants:
            if grant.mode != "readwrite":
                continue
            _admit(grant.path)
        for target in verdict.write_paths:
            root = _one_shot_write_root(target)
            if root:
                _admit(root)
        return {
            "reasons": verdict.reasons,
            "local_command": LocalCommandAuthorization(
                command=intent.target,
                approval_mode=approval_mode,
                write_paths=tuple(write_paths),
            ),
        }

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
        # 权限档绑到执行期上下文：工具体内自己发起的确认（bash 回写「我的空间」）
        # 不经过这里的判定，但必须看到同一档位。
        mode_token = CURRENT_APPROVAL_MODE.set(self.service.runtime.approval_mode)
        try:
            async for item in self._act(input_kwargs, next_handler):
                yield item
        finally:
            CURRENT_APPROVAL_MODE.reset(mode_token)

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
            CURRENT_PERMISSION_TICKET.reset(token)


__all__ = [
    "CONFINE_NONE",
    "CONFINE_PREFERRED",
    "CONFINE_REQUIRED",
    "CURRENT_APPROVAL_MODE",
    "CURRENT_PERMISSION_TICKET",
    "FALLBACK_ALLOW",
    "FALLBACK_DENY",
    "LOCAL_CONFINEMENT_BY_MODE",
    "ConfinedCommand",
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
    "preset_answers_confirmation",
    "register_mcp_client_permissions",
    "require_local_path_permission",
    "resolve_approval_mode",
]
