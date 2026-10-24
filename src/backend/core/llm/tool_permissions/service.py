"""Resource policy evaluation; callers supply one immutable dispatch runtime."""

from __future__ import annotations
import logging
import os
from typing import Any, Sequence, Optional
from core.sandbox.permissions.approval import DESTRUCTIVE_OPS, _preset_answers
from core.sandbox.permissions.tool_specs import ToolPermissionRegistry, _one_shot_write_root
from core.sandbox.permissions.dispatch_models import DOMAIN_APPROVAL, DOMAIN_DENY, DOMAIN_LOCAL_COMMAND, DOMAIN_LOCAL_PATH, DOMAIN_MYSPACE, FAIL_CLOSED_MODE, FALLBACK_ALLOW, LocalCommandAuthorization, PermissionIntent, PermissionOutcome, PermissionRuntime, PermissionTicket, READ, UNCONFINED_APPROVAL_MODES, _call_args, _json_hash

logger = logging.getLogger(__name__)


class ToolPermissionService:
    """Policy decision point for explicitly governed native and MCP tools."""

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
            from core.llm.tools.project_source_access import current_scope_error

            scope = self.runtime.project_scope
            scope_error = (
                current_scope_error(scope, self.runtime.user_id)
                if scope is not None and scope.is_local
                else None
            )
            if scope_error:
                return PermissionOutcome(False, payload=self._blocked(scope_error["error"]))
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
        from core.sandbox.permissions.models import DELETE
        from core.sandbox.permissions.models import NETWORK
        from core.sandbox.permissions.models import PRIVILEGE
        from core.sandbox.permissions.models import SYSTEM_WRITE
        from core.sandbox.permissions.models import Policy

        mode = self.runtime.approval_mode
        try:
            from core.harness.registry.local_files import grants_for_gate, policy_for_gate

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

    def _execution_directory(self) -> str:
        from core.sandbox.workspace.resolution import workspace_directory

        return workspace_directory(
            self.runtime.sandbox_session_id or self.runtime.chat_id,
            scope=self.runtime.project_scope,
        )

    def _session_workspace(self) -> str:
        """会话存储目录及默认权限根；与项目执行 cwd 分开。

        项目目录仍按用户现有授权判断；切换 cwd 不新增隐式文件权限。
        """
        from core.sandbox.workspace.paths import WORKSPACE
        from core.sandbox.workspace.paths import session_root

        session = self.runtime.sandbox_session_id or self.runtime.chat_id
        return session_root(WORKSPACE, str(session)) if session else WORKSPACE

    async def _authorize_local_path(self, intent: PermissionIntent) -> dict[str, Any]:
        from core.config.local_mode import local_mode_enabled

        if not local_mode_enabled():
            return {}
        from core.sandbox.permissions.models import danger_categories
        from core.sandbox.permissions.evaluator import evaluate_local_path

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

        from core.llm.tools import confirmation as confirm

        op = confirm.OP_LOCAL_READ if intent.action == READ else confirm.OP_LOCAL_WRITE
        blocked = await confirm.gate(
            chat_id=self.runtime.chat_id,
            approval_user_id=self.runtime.user_id if self.runtime.account_approval else None,
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
        from core.sandbox.workspace.paths import WORKSPACE
        from core.sandbox.permissions.models import Grant
        from core.sandbox.permissions.models import danger_categories
        from core.sandbox.permissions.evaluator import evaluate_local_command

        platform = "windows" if os.name == "nt" else "posix"
        approval_mode, grants, policy = self._safe_local_security()
        eval_grants = list(grants)
        if WORKSPACE != "/workspace":
            eval_grants.append(Grant(WORKSPACE, "readwrite"))
        verdict = evaluate_local_command(
            intent.target,
            cwd=self._execution_directory(),
            grants=eval_grants,
            policy=policy,
            workspace_root=self._session_workspace(),
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
            from core.llm.tools import confirmation as confirm

            blocked = await confirm.gate(
                chat_id=self.runtime.chat_id,
                approval_user_id=self.runtime.user_id if self.runtime.account_approval else None,
                op=confirm.OP_LOCAL_EXEC,
                logical_path=intent.target[:160],
                interactive=bool(self.runtime.approval_available and self.runtime.chat_id),
                summary=intent.summary
                + (f"（{'、'.join(verdict.reasons)}）" if verdict.reasons else ""),
                kind=confirm.KIND_LOCAL_CMD,
            )
            if blocked is not None:
                return {"payload": blocked, "reasons": verdict.reasons}

        import asyncio

        from core.sandbox.permissions.os_gate import capability_skill_roots

        # Globbing the capability store is file I/O; keep it off the event loop.
        skill_roots = await asyncio.to_thread(capability_skill_roots)
        return {
            "reasons": verdict.reasons,
            "local_command": self._local_command_authorization(
                command=intent.target,
                approval_mode=approval_mode,
                grants=grants,
                policy=policy,
                one_shot_write_targets=tuple(verdict.write_paths),
                skill_roots=skill_roots,
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
            from core.sandbox.workspace.resolution import to_physical_path

            session = self.runtime.sandbox_session_id or self.runtime.chat_id
            return to_physical_path(
                logical, self.runtime.user_id, session_id=session, scope=self.runtime.project_scope
            )
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
        skill_roots: Sequence[str] = (),
    ) -> LocalCommandAuthorization:
        """Turn this run's permission configuration into a sandbox scope.

        Every local command goes through here, prompted or not, so an unattended
        entry point is confined exactly like an interactive one — it only skips
        the asking, never the sandbox.
        """
        from core.sandbox.workspace.paths import WORKSPACE
        from core.sandbox.permissions.models import NETWORK
        from core.sandbox.permissions.models import SYSTEM_WRITE
        from core.sandbox.permissions.paths import intersects_system_write_area
        from core.sandbox.permissions.os_gate import LocalAccessDecision

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

        from core.sandbox.permissions.os_gate import protected_read_paths

        # Skill scripts live in the protected capability store; only their own
        # directories are reopened, and only for reading.
        readable.extend(skill_roots)

        return LocalCommandAuthorization(
            command=command,
            approval_mode=approval_mode,
            workspace_root=self._session_workspace(),
            cwd=self._execution_directory(),
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
        from core.llm.tools import confirmation as confirm

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
            approval_user_id=self.runtime.user_id if self.runtime.account_approval else None,
            op=intent.op or intent.action,
            logical_path=intent.target,
            interactive=bool(interactive and self.runtime.chat_id),
            summary=intent.summary,
            kind=intent.kind or confirm.KIND_TOOL_PERMISSION,
        )
        return {"payload": blocked} if blocked is not None else {}
