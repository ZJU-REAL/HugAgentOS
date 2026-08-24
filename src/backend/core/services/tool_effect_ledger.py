"""Durable intent-before-invocation gateway for tool effects.

``ToolEffectLedger`` is append-only and authoritative. The companion lease
row is only a mutable execution/recovery claim. All public execution paths go
through :class:`ToolEffectGateway`; adapters are never called until an Intent
has committed and an invocation guard has rechecked ownership.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, cast

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from core.db.engine import SessionLocal
from core.db.models import ChatRun, ToolEffectLease, ToolEffectLedger
from core.infra.data_masking import mask_sensitive_data
from core.services.run_journal import (
    LIVE_STATUSES,
    RunJournal,
    RunLeaseLost,
    RunNotFound,
)

logger = logging.getLogger(__name__)

RECOVERY_POLICIES = ("replay_safe", "reconcile", "never_replay")
_REDACT_FIELDS = [
    "password",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "access_key",
    "cookie",
]
_WITHHOLD_FIELDS = {"command", "content", "stdin", "script", "code"}
_EMBEDDED_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s'\";]+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)([?&](?:token|api_key|apikey|access_key|secret|password|passwd|client_secret|authorization)=)[^&#\s]+"
    ),
    re.compile(
        r"(?i)((?:token|api_key|apikey|access_key|secret|password|passwd|client_secret|authorization)\s*[=:]\s*)[^\s'\";]+"
    ),
)


class ToolEffectError(RuntimeError):
    pass


class ToolIntentCommitError(ToolEffectError):
    """The intent could not be durably committed; invocation is forbidden."""


class ToolOutcomeUnknown(ToolEffectError):
    """The ledger proves intent but cannot prove the external outcome."""


def find_tool_outcome_unknown(exc: BaseException) -> Optional[ToolOutcomeUnknown]:
    """Unwrap AgentScope/anyio ExceptionGroup layers without losing safety meaning."""
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ToolOutcomeUnknown):
            return current
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        if isinstance(current.__cause__, BaseException):
            pending.append(current.__cause__)
        if isinstance(current.__context__, BaseException):
            pending.append(current.__context__)
    return None


class ToolEffectLeaseLost(ToolEffectError):
    pass


class ToolIdempotencyConflict(ToolEffectError):
    """A caller reused one explicit key for different logical input."""


@dataclass(frozen=True)
class ReconciliationResult:
    outcome: str
    result: Any = None

    @classmethod
    def applied(cls, result: Any) -> "ReconciliationResult":
        return cls("applied", result)

    @classmethod
    def not_applied(cls) -> "ReconciliationResult":
        return cls("not_applied")

    @classmethod
    def unknown(cls) -> "ReconciliationResult":
        return cls("unknown")


@dataclass(frozen=True)
class ToolPolicy:
    policy: str
    replayer: Optional[Callable[["ToolIntent"], Awaitable[Any] | Any]] = None
    reconciler: Optional[
        Callable[["ToolIntent"], Awaitable[ReconciliationResult | Any] | Any]
    ] = None


@dataclass(frozen=True)
class ToolIntent:
    effect_id: str
    result_id: str
    run_id: str
    operation_seq: int
    tool_name: str
    tool_call_id: str
    args_hash: str
    redacted_args: Mapping[str, Any]
    idempotency_key: str
    recovery_policy: str
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class IntentDecision:
    action: str
    intent: ToolIntent
    result: Any = None
    result_state: str = "success"

    @property
    def effect_id(self) -> str:
        return self.intent.effect_id


@dataclass(frozen=True)
class ToolExecutionOutcome:
    result: Any
    effect_id: str
    result_id: str
    result_state: str
    invoked: bool


@dataclass(frozen=True)
class RecoverySweepDecision:
    effect_id: str
    run_id: str
    action: str


@dataclass(frozen=True)
class ToolInvocationContext:
    effect_id: str
    result_id: str
    idempotency_key: str
    run_id: str
    fencing_owner: str


CURRENT_TOOL_EFFECT: ContextVar[Optional[ToolInvocationContext]] = ContextVar(
    "jx_current_tool_effect", default=None
)


class ToolRecoveryRegistry:
    """Explicit adapter contracts. Unknown tools fail closed as never-replay."""

    def __init__(self) -> None:
        self._policies: dict[str, ToolPolicy] = {}

    def register(
        self,
        tool_name: str,
        policy: str,
        *,
        replayer: Optional[Callable[[ToolIntent], Awaitable[Any] | Any]] = None,
        reconciler: Optional[
            Callable[[ToolIntent], Awaitable[ReconciliationResult | Any] | Any]
        ] = None,
    ) -> None:
        if policy not in RECOVERY_POLICIES:
            raise ValueError(f"invalid recovery policy: {policy}")
        if policy == "replay_safe" and reconciler is not None:
            raise ValueError("replay_safe does not accept a reconciler")
        if policy == "never_replay" and (
            replayer is not None or reconciler is not None
        ):
            raise ValueError("never_replay does not accept recovery adapters")
        self._policies[str(tool_name)] = ToolPolicy(policy, replayer, reconciler)

    def resolve(self, tool_name: str) -> ToolPolicy:
        return self._policies.get(str(tool_name), ToolPolicy("never_replay"))


async def _default_replayer(intent: ToolIntent) -> Any:
    from orchestration.tool_effect_recovery import replay_tool_intent

    return await replay_tool_intent(intent)


async def _scheduled_task_reconciler(intent: ToolIntent) -> ReconciliationResult:
    from orchestration.tool_effect_recovery import reconcile_scheduled_task_intent

    return cast(ReconciliationResult, await reconcile_scheduled_task_intent(intent))


def build_default_tool_recovery_registry() -> ToolRecoveryRegistry:
    registry = ToolRecoveryRegistry()
    for name in (
        "Read",
        "Glob",
        "Grep",
        "view_text_file",
        "get_data_context",
        "view_image",
        "read_artifact",
        "list_myspace_files",
        "stage_myspace_file",
        "list_favorite_chats",
        "get_chat_messages",
        "channel_read_attachment",
        "sandbox_get_artifact",
    ):
        registry.register(name, "replay_safe", replayer=_default_replayer)
    for name in (
        "Write",
        "Edit",
        "Delete",
        "Move",
        "CreateFolder",
        "bash",
        "Bash",
        "sandbox_put_artifact",
        "pin_to_workspace",
        "run_job",
        "choose_design",
        "call_subagent",
        "update_plan",
        "load_plugin",
    ):
        registry.register(name, "never_replay")
    for name in (
        "create_scheduled_task",
        "update_scheduled_task",
        "delete_scheduled_task",
    ):
        registry.register(
            name,
            "reconcile",
            replayer=_default_replayer,
            reconciler=_scheduled_task_reconciler,
        )
    return registry


DEFAULT_TOOL_RECOVERY_REGISTRY = build_default_tool_recovery_registry()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _embedded_redact(value: str) -> str:
    cleaned = value
    for pattern in _EMBEDDED_SECRET_PATTERNS:
        cleaned = pattern.sub(r"\1[REDACTED]", cleaned)
    return cleaned


def _sanitize_value(value: Any, *, key: str = "") -> Any:
    lower = key.lower()
    if isinstance(value, Mapping):
        return {str(k): _sanitize_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item, key=key) for item in value]
    if isinstance(value, str):
        if lower in _WITHHOLD_FIELDS:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            return {"_withheld": True, "sha256": digest, "length": len(value)}
        return _embedded_redact(value)
    return value


def canonical_tool_args(args: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    canonical = json.dumps(
        args or {}, sort_keys=True, separators=(",", ":"), default=str
    )
    args_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    by_field = mask_sensitive_data(dict(args or {}), field_patterns=_REDACT_FIELDS)
    return canonical, args_hash, _sanitize_value(by_field)


def tool_args_hash(args: Mapping[str, Any]) -> str:
    return canonical_tool_args(args)[1]


def stable_idempotency_key(run_id: str, tool_name: str, invocation_id: str) -> str:
    raw = f"{run_id}\x00{tool_name}\x00{invocation_id}".encode("utf-8")
    return "tool:" + hashlib.sha256(raw).hexdigest()


def _resolved_idempotency_key(
    *,
    run_id: str,
    principal_scope: str,
    tool_name: str,
    tool_call_id: str,
    explicit_key: Optional[str],
) -> tuple[str, bool]:
    caller_key = str(explicit_key or "").strip()
    if caller_key:
        scoped = f"{principal_scope}\x00{caller_key}".encode("utf-8")
        return "caller:" + hashlib.sha256(scoped).hexdigest(), True
    if not tool_call_id:
        raise ToolIntentCommitError(
            "tool_call_id or explicit idempotency_key is required"
        )
    return stable_idempotency_key(run_id, tool_name, tool_call_id), False


class ToolEffectJournal:
    """Small synchronous transaction boundary behind the async gateway."""

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._runs = RunJournal(session_factory, clock=clock)

    @staticmethod
    def _intent_from_row(row: ToolEffectLedger) -> ToolIntent:
        return ToolIntent(
            effect_id=row.effect_id,
            result_id=row.result_id,
            run_id=row.run_id,
            operation_seq=int(row.operation_seq),
            tool_name=row.tool_name,
            tool_call_id=row.tool_call_id or "",
            args_hash=row.args_hash,
            redacted_args=dict(row.redacted_args or {}),
            idempotency_key=row.idempotency_key or "",
            recovery_policy=row.recovery_policy,
            created_at=_aware(row.created_at),
        )

    @staticmethod
    def _terminal_row(db: Session, effect_id: str) -> Optional[ToolEffectLedger]:
        return (
            db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.terminal_effect_id == effect_id)
            .one_or_none()
        )

    @staticmethod
    def _decision_from_terminal(
        intent: ToolIntent, terminal: ToolEffectLedger
    ) -> IntentDecision:
        if terminal.event_type == "unknown_outcome":
            return IntentDecision("unknown", intent, result_state="unknown")
        return IntentDecision(
            "known",
            intent,
            terminal.result_payload,
            "success" if terminal.event_type == "result" else "error",
        )

    @staticmethod
    def _validate_identity(
        row: ToolEffectLedger, *, tool_name: str, args_hash: str
    ) -> None:
        if row.tool_name != tool_name or row.args_hash != args_hash:
            raise ToolIdempotencyConflict(
                "idempotency_key already belongs to different tool arguments"
            )

    def _append_event(
        self,
        db: Session,
        run: ChatRun,
        intent: ToolEffectLedger,
        *,
        owner: str,
        event_type: str,
        operation_type: str,
        phase: str,
        safety: str,
        result: Any = None,
        error: Optional[str] = None,
        terminal: bool = False,
    ) -> None:
        receipt = self._runs._append_locked(
            db,
            run,
            owner=owner,
            operation_type=operation_type,
            phase=phase,
            safety=safety,
            payload={"effect_id": intent.effect_id, "result_id": intent.result_id},
        )
        db.add(
            ToolEffectLedger(
                effect_id=intent.effect_id,
                result_id=intent.result_id,
                run_id=intent.run_id,
                operation_seq=receipt.operation_seq,
                event_type=event_type,
                tool_name=intent.tool_name,
                tool_call_id=intent.tool_call_id,
                args_hash=intent.args_hash,
                redacted_args=intent.redacted_args,
                recovery_policy=intent.recovery_policy,
                result_payload=result,
                error_message=error,
                terminal_effect_id=intent.effect_id if terminal else None,
            )
        )

    def _mark_unknown_locked(
        self,
        db: Session,
        run: ChatRun,
        intent: ToolEffectLedger,
        lease: ToolEffectLease,
        *,
        owner: str,
        claim_owner: str,
        reason: str,
    ) -> None:
        if self._terminal_row(db, intent.effect_id) is not None:
            return
        if lease.run_owner != owner or lease.claim_owner != claim_owner:
            raise ToolEffectLeaseLost(intent.effect_id)
        self._append_event(
            db,
            run,
            intent,
            owner=owner,
            event_type="unknown_outcome",
            operation_type="tool_outcome_unknown",
            phase="needs_attention",
            safety="manual_review",
            error=reason,
            terminal=True,
        )
        run.status = "needs_attention"
        run.failure_reason = reason
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = self._clock()
        lease.claim_owner = None
        lease.lease_expires_at = None
        lease.updated_at = self._clock()

    def _existing_decision(
        self,
        *,
        key: str,
        caller_run_id: str,
        run_owner: str,
        claim_owner: str,
        tool_name: str,
        args_hash: str,
        lease_seconds: int,
    ) -> IntentDecision:
        with self._sessions() as db:
            intent_row = (
                db.query(ToolEffectLedger)
                .filter(ToolEffectLedger.idempotency_key == key)
                .one()
            )
            self._validate_identity(
                intent_row, tool_name=tool_name, args_hash=args_hash
            )
            intent = self._intent_from_row(intent_row)
            caller_run = db.get(ChatRun, caller_run_id)
            original_run = db.get(ChatRun, intent.run_id)
            if caller_run is None or original_run is None:
                raise ToolIntentCommitError(f"run does not exist: {caller_run_id}")
            if original_run.user_id != caller_run.user_id:
                raise ToolIdempotencyConflict(
                    "idempotency_key belongs to a different principal"
                )
            terminal = self._terminal_row(db, intent.effect_id)
            if terminal is not None and caller_run_id == intent.run_id:
                return self._decision_from_terminal(intent, terminal)
            now = _aware(self._clock())
            caller_expiry = _aware(caller_run.lease_expires_at)
            if (
                caller_run.status not in LIVE_STATUSES
                or caller_run.lease_owner != run_owner
                or caller_expiry is None
                or now is None
                or caller_expiry <= now
            ):
                raise ToolIntentCommitError(f"run lease is not owned: {caller_run_id}")
            if terminal is not None:
                return self._decision_from_terminal(intent, terminal)
            if intent.run_id != caller_run_id:
                return IntentDecision("wait", intent)

            lease = (
                db.query(ToolEffectLease)
                .filter(ToolEffectLease.effect_id == intent.effect_id)
                .with_for_update()
                .one()
            )
            run = self._runs._locked_owned_run(db, caller_run_id, run_owner)
            terminal = self._terminal_row(db, intent.effect_id)
            if terminal is not None:
                return self._decision_from_terminal(intent, terminal)
            lease_expiry = _aware(lease.lease_expires_at)
            if (
                lease.run_owner == run_owner
                and lease_expiry is not None
                and lease_expiry > now
            ):
                if lease.claim_owner == claim_owner:
                    return IntentDecision(
                        "reconcile"
                        if intent.recovery_policy == "reconcile"
                        else "execute",
                        intent,
                    )
                return IntentDecision("wait", intent)

            previous_owner = lease.run_owner
            previous_claim = lease.claim_owner
            previous_expiry = lease.lease_expires_at
            affected = (
                db.query(ToolEffectLease)
                .filter(
                    ToolEffectLease.effect_id == intent.effect_id,
                    ToolEffectLease.run_owner == previous_owner,
                    ToolEffectLease.claim_owner == previous_claim,
                    ToolEffectLease.lease_expires_at == previous_expiry,
                    or_(
                        ToolEffectLease.lease_expires_at.is_(None),
                        ToolEffectLease.lease_expires_at <= self._clock(),
                    ),
                )
                .update(
                    {
                        ToolEffectLease.run_owner: run_owner,
                        ToolEffectLease.claim_owner: claim_owner,
                        ToolEffectLease.lease_expires_at: self._clock()
                        + timedelta(seconds=max(1, lease_seconds)),
                        ToolEffectLease.updated_at: self._clock(),
                    },
                    synchronize_session=False,
                )
            )
            if not affected:
                db.rollback()
                return IntentDecision("wait", intent)
            db.expire_all()
            lease = db.get(ToolEffectLease, intent.effect_id)
            self._append_event(
                db,
                run,
                intent_row,
                owner=run_owner,
                event_type="recovery_claim",
                operation_type="tool_effect_recovery_claimed",
                phase=f"tool_intent_{intent.recovery_policy}",
                safety=intent.recovery_policy,
            )
            if intent.recovery_policy == "never_replay":
                self._mark_unknown_locked(
                    db,
                    run,
                    intent_row,
                    lease,
                    owner=run_owner,
                    claim_owner=claim_owner,
                    reason="non-replayable tool intent has no committed result",
                )
                db.commit()
                return IntentDecision("unknown", intent)
            db.commit()
            return IntentDecision(
                "reconcile" if intent.recovery_policy == "reconcile" else "execute",
                intent,
            )

    def begin_intent(
        self,
        *,
        run_id: str,
        owner: str,
        claim_owner: str,
        tool_call_id: str,
        tool_name: str,
        args: Mapping[str, Any],
        recovery_policy: str,
        idempotency_key: Optional[str] = None,
        lease_seconds: int = 300,
        _conflict_retries: int = 4,
    ) -> IntentDecision:
        if recovery_policy not in RECOVERY_POLICIES:
            raise ValueError(f"invalid recovery policy: {recovery_policy}")
        _canonical, args_hash, redacted = canonical_tool_args(args)
        with self._sessions() as db:
            caller_run = db.get(ChatRun, run_id)
            if caller_run is None:
                raise ToolIntentCommitError(f"run does not exist: {run_id}")
            principal_scope = caller_run.user_id
        key, _explicit = _resolved_idempotency_key(
            run_id=run_id,
            principal_scope=str(principal_scope),
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            explicit_key=idempotency_key
            or str(args.get("idempotency_key") or args.get("request_id") or "").strip(),
        )
        try:
            with self._sessions() as db:
                found = (
                    db.query(ToolEffectLedger.event_id)
                    .filter(ToolEffectLedger.idempotency_key == key)
                    .first()
                )
            if found is not None:
                return self._existing_decision(
                    key=key,
                    caller_run_id=run_id,
                    run_owner=owner,
                    claim_owner=claim_owner,
                    tool_name=tool_name,
                    args_hash=args_hash,
                    lease_seconds=lease_seconds,
                )

            with self._sessions() as db:
                run = self._runs._locked_owned_run(db, run_id, owner)
                found = (
                    db.query(ToolEffectLedger.event_id)
                    .filter(ToolEffectLedger.idempotency_key == key)
                    .first()
                )
                if found is not None:
                    db.rollback()
                    return self._existing_decision(
                        key=key,
                        caller_run_id=run_id,
                        run_owner=owner,
                        claim_owner=claim_owner,
                        tool_name=tool_name,
                        args_hash=args_hash,
                        lease_seconds=lease_seconds,
                    )
                effect_id = f"eff_{uuid.uuid4().hex}"
                result_id = f"toolres_{uuid.uuid4().hex}"
                receipt = self._runs._append_locked(
                    db,
                    run,
                    owner=owner,
                    operation_type="tool_intent_committed",
                    phase=f"tool_intent_{recovery_policy}",
                    safety=recovery_policy,
                    payload={
                        "effect_id": effect_id,
                        "result_id": result_id,
                        "tool_name": tool_name,
                        "tool_call_id": tool_call_id,
                        "args_hash": args_hash,
                    },
                )
                row = ToolEffectLedger(
                    effect_id=effect_id,
                    result_id=result_id,
                    run_id=run_id,
                    operation_seq=receipt.operation_seq,
                    event_type="intent",
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    args_hash=args_hash,
                    redacted_args=dict(redacted),
                    idempotency_key=key,
                    recovery_policy=recovery_policy,
                )
                db.add(row)
                db.add(
                    ToolEffectLease(
                        effect_id=effect_id,
                        run_id=run_id,
                        run_owner=owner,
                        claim_owner=claim_owner,
                        lease_expires_at=self._clock()
                        + timedelta(seconds=max(1, lease_seconds)),
                        updated_at=self._clock(),
                    )
                )
                run.updated_at = self._clock()
                db.commit()
                return IntentDecision("execute", self._intent_from_row(row))
        except ToolEffectError:
            raise
        except (IntegrityError, OperationalError) as exc:
            with self._sessions() as db:
                found = (
                    db.query(ToolEffectLedger.event_id)
                    .filter(ToolEffectLedger.idempotency_key == key)
                    .first()
                )
            if found is not None:
                return self._existing_decision(
                    key=key,
                    caller_run_id=run_id,
                    run_owner=owner,
                    claim_owner=claim_owner,
                    tool_name=tool_name,
                    args_hash=args_hash,
                    lease_seconds=lease_seconds,
                )
            if _conflict_retries > 0:
                return self.begin_intent(
                    run_id=run_id,
                    owner=owner,
                    claim_owner=claim_owner,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    args=args,
                    recovery_policy=recovery_policy,
                    idempotency_key=idempotency_key,
                    lease_seconds=lease_seconds,
                    _conflict_retries=_conflict_retries - 1,
                )
            raise ToolIntentCommitError(str(exc)) from exc
        except (RunNotFound, RunLeaseLost) as exc:
            raise ToolIntentCommitError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise ToolIntentCommitError(str(exc)) from exc

    def acquire_invocation_claim(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
        lease_seconds: int = 300,
    ) -> None:
        """Validate and extend one invocation claim in a short atomic transaction."""
        now = self._clock()
        expires = now + timedelta(seconds=max(1, lease_seconds))
        with self._sessions() as db:
            live_run = db.query(ChatRun.run_id).filter(
                ChatRun.status.in_(LIVE_STATUSES),
                ChatRun.lease_owner == run_owner,
                ChatRun.lease_expires_at > now,
            )
            affected = (
                db.query(ToolEffectLease)
                .filter(
                    ToolEffectLease.effect_id == effect_id,
                    ToolEffectLease.run_owner == run_owner,
                    ToolEffectLease.claim_owner == claim_owner,
                    ToolEffectLease.lease_expires_at > now,
                    ToolEffectLease.run_id.in_(live_run),
                )
                .update(
                    {
                        ToolEffectLease.lease_expires_at: expires,
                        ToolEffectLease.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if not affected or self._terminal_row(db, effect_id) is not None:
                db.rollback()
                raise ToolEffectLeaseLost(effect_id)
            db.commit()

    def renew_invocation_claim(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
        lease_seconds: int,
    ) -> bool:
        if not self._runs.renew(
            self._intent_run_id(effect_id),
            owner=run_owner,
            lease_seconds=lease_seconds,
        ):
            return False
        try:
            self.acquire_invocation_claim(
                effect_id,
                run_owner=run_owner,
                claim_owner=claim_owner,
                lease_seconds=lease_seconds,
            )
            return True
        except ToolEffectLeaseLost:
            return False

    def renew_recovery_claim(
        self,
        effect_id: str,
        *,
        recovery_owner: str,
        lease_seconds: int,
    ) -> bool:
        return self.renew_invocation_claim(
            effect_id,
            run_owner=recovery_owner,
            claim_owner=recovery_owner,
            lease_seconds=lease_seconds,
        )

    def _intent_run_id(self, effect_id: str) -> str:
        with self._sessions() as db:
            row = (
                db.query(ToolEffectLedger.run_id)
                .filter(
                    ToolEffectLedger.effect_id == effect_id,
                    ToolEffectLedger.event_type == "intent",
                )
                .one()
            )
            return str(row[0])

    def abandon_invocation_claim(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
    ) -> None:
        """Expire a cancelled/ambiguous call so policy recovery can take over."""
        now = self._clock()
        with self._sessions() as db:
            db.query(ToolEffectLease).filter(
                ToolEffectLease.effect_id == effect_id,
                ToolEffectLease.run_owner == run_owner,
                ToolEffectLease.claim_owner == claim_owner,
            ).update(
                {
                    ToolEffectLease.lease_expires_at: now,
                    ToolEffectLease.updated_at: now,
                },
                synchronize_session=False,
            )
            db.commit()

    def _commit_result_locked(
        self,
        db: Session,
        intent: ToolEffectLedger,
        lease: ToolEffectLease,
        *,
        owner: str,
        result: Any,
        failed: bool,
        reconciled: bool,
    ) -> Any:
        terminal = self._terminal_row(db, intent.effect_id)
        if terminal is not None:
            if terminal.event_type == "unknown_outcome":
                raise ToolOutcomeUnknown(intent.effect_id)
            return terminal.result_payload
        if lease.run_owner != owner:
            raise ToolEffectLeaseLost(intent.effect_id)
        run = self._runs._locked_owned_run(db, intent.run_id, owner)
        self._append_event(
            db,
            run,
            intent,
            owner=owner,
            event_type="failure" if failed else "result",
            operation_type="tool_failure_committed"
            if failed
            else "tool_result_committed",
            phase="tool_result_committed",
            safety="reconciled" if reconciled else intent.recovery_policy,
            result=result,
            error=str(result)[:4000] if failed else None,
            terminal=True,
        )
        lease.claim_owner = None
        lease.lease_expires_at = None
        lease.updated_at = self._clock()
        run.updated_at = self._clock()
        return result

    def commit_result(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
        result: Any,
        failed: bool = False,
        reconciled: bool = False,
        _conflict_retries: int = 4,
    ) -> Any:
        with self._sessions() as db:
            intent = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.effect_id == effect_id,
                    ToolEffectLedger.event_type == "intent",
                )
                .one()
            )
            terminal = self._terminal_row(db, effect_id)
            if terminal is not None:
                if terminal.event_type == "unknown_outcome":
                    raise ToolOutcomeUnknown(effect_id)
                return terminal.result_payload
            lease = (
                db.query(ToolEffectLease)
                .filter(ToolEffectLease.effect_id == effect_id)
                .with_for_update()
                .one()
            )
            now = _aware(self._clock())
            lease_expiry = _aware(lease.lease_expires_at)
            if (
                lease.run_owner != run_owner
                or lease.claim_owner != claim_owner
                or lease_expiry is None
                or now is None
                or lease_expiry <= now
            ):
                # A concurrent retry can observe the lease only after the
                # winning commit cleared its claim. Re-check the durable
                # terminal decision before reporting lease loss: identical
                # result commits are idempotent even when the first pre-lock
                # terminal read raced with the winner.
                terminal = self._terminal_row(db, effect_id)
                if terminal is not None:
                    if terminal.event_type == "unknown_outcome":
                        raise ToolOutcomeUnknown(effect_id)
                    return terminal.result_payload
                raise ToolEffectLeaseLost(effect_id)
            value = self._commit_result_locked(
                db,
                intent,
                lease,
                owner=run_owner,
                result=result,
                failed=failed,
                reconciled=reconciled,
            )
            try:
                db.commit()
            except (IntegrityError, OperationalError) as exc:
                db.rollback()
                terminal = self.terminal_decision(effect_id)
                if terminal is not None:
                    if terminal.action == "unknown":
                        raise ToolOutcomeUnknown(effect_id)
                    return terminal.result
                if _conflict_retries > 0:
                    return self.commit_result(
                        effect_id,
                        run_owner=run_owner,
                        claim_owner=claim_owner,
                        result=result,
                        failed=failed,
                        reconciled=reconciled,
                        _conflict_retries=_conflict_retries - 1,
                    )
                raise ToolEffectError(str(exc)) from exc
            return value

    def mark_unknown(
        self,
        effect_id: str,
        *,
        owner: str,
        claim_owner: str,
        reason: str,
    ) -> None:
        with self._sessions() as db:
            lease = (
                db.query(ToolEffectLease)
                .filter(ToolEffectLease.effect_id == effect_id)
                .with_for_update()
                .one()
            )
            intent = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.effect_id == effect_id,
                    ToolEffectLedger.event_type == "intent",
                )
                .one()
            )
            run = self._runs._locked_owned_run(db, intent.run_id, owner)
            self._mark_unknown_locked(
                db,
                run,
                intent,
                lease,
                owner=owner,
                claim_owner=claim_owner,
                reason=reason,
            )
            db.commit()

    def terminal_decision(self, effect_id: str) -> Optional[IntentDecision]:
        with self._sessions() as db:
            intent = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.effect_id == effect_id,
                    ToolEffectLedger.event_type == "intent",
                )
                .one_or_none()
            )
            if intent is None:
                return None
            terminal = self._terminal_row(db, effect_id)
            return (
                self._decision_from_terminal(self._intent_from_row(intent), terminal)
                if terminal is not None
                else None
            )

    def pending_effect_ids(self) -> list[str]:
        with self._sessions() as db:
            rows = (
                db.query(ToolEffectLedger)
                .filter(ToolEffectLedger.event_type == "intent")
                .order_by(ToolEffectLedger.created_at, ToolEffectLedger.event_id)
                .all()
            )
            return [
                row.effect_id
                for row in rows
                if self._terminal_row(db, row.effect_id) is None
            ]

    def settle_terminal_run_intent(self, effect_id: str) -> Optional[ToolIntent]:
        """Close an orphan intent without replay after its owning run is terminal.

        Cancellation/failure is a hard replay boundary: the user's stopped run
        must never perform a delayed external action.  We still append an
        ``unknown_outcome`` fact so the intent cannot remain pending forever.
        """
        with self._sessions() as db:
            intent = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.effect_id == effect_id,
                    ToolEffectLedger.event_type == "intent",
                )
                .with_for_update()
                .one_or_none()
            )
            if intent is None or self._terminal_row(db, effect_id) is not None:
                return None
            run = (
                db.query(ChatRun)
                .filter(ChatRun.run_id == intent.run_id)
                .with_for_update()
                .one_or_none()
            )
            if run is None or run.status in (*LIVE_STATUSES, "needs_attention"):
                return None
            lease = (
                db.query(ToolEffectLease)
                .filter(ToolEffectLease.effect_id == effect_id)
                .with_for_update()
                .one()
            )
            terminal_status = str(run.status)
            self._append_event(
                db,
                run,
                intent,
                owner="system:terminal-settlement",
                event_type="unknown_outcome",
                operation_type="tool_outcome_unknown_after_terminal_run",
                phase=str(run.run_phase or terminal_status),
                safety="manual_review",
                error=f"run became {terminal_status} before the tool outcome committed",
                terminal=True,
            )
            # _append_event advances only the operation ledger. Preserve the
            # authoritative terminal verdict and merely close the effect claim.
            run.status = terminal_status
            lease.claim_owner = None
            lease.lease_expires_at = None
            lease.updated_at = self._clock()
            run.updated_at = self._clock()
            db.commit()
            return self._intent_from_row(intent)

    def claim_recovery_intent(
        self,
        effect_id: str,
        *,
        recovery_owner: str,
        lease_seconds: int = 120,
    ) -> Optional[ToolIntent]:
        with self._sessions() as db:
            intent = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.effect_id == effect_id,
                    ToolEffectLedger.event_type == "intent",
                )
                .one_or_none()
            )
            if intent is None or self._terminal_row(db, effect_id) is not None:
                return None
            now = self._clock()
            expires = now + timedelta(seconds=max(1, lease_seconds))
            run_affected = (
                db.query(ChatRun)
                .filter(
                    ChatRun.run_id == intent.run_id,
                    ChatRun.status.in_((*LIVE_STATUSES, "needs_attention")),
                    or_(
                        ChatRun.lease_owner.is_(None),
                        ChatRun.lease_expires_at.is_(None),
                        ChatRun.lease_expires_at <= now,
                        ChatRun.lease_owner == recovery_owner,
                    ),
                )
                .update(
                    {
                        ChatRun.status: "running",
                        ChatRun.lease_owner: recovery_owner,
                        ChatRun.lease_expires_at: expires,
                        ChatRun.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if not run_affected:
                db.rollback()
                return None
            lease_affected = (
                db.query(ToolEffectLease)
                .filter(
                    ToolEffectLease.effect_id == effect_id,
                    or_(
                        ToolEffectLease.lease_expires_at.is_(None),
                        ToolEffectLease.lease_expires_at <= now,
                        ToolEffectLease.claim_owner == recovery_owner,
                    ),
                )
                .update(
                    {
                        ToolEffectLease.run_owner: recovery_owner,
                        ToolEffectLease.claim_owner: recovery_owner,
                        ToolEffectLease.lease_expires_at: expires,
                        ToolEffectLease.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if not lease_affected or self._terminal_row(db, effect_id) is not None:
                db.rollback()
                return None
            db.expire_all()
            run = db.get(ChatRun, intent.run_id)
            self._append_event(
                db,
                run,
                intent,
                owner=recovery_owner,
                event_type="recovery_claim",
                operation_type="tool_effect_recovery_claimed",
                phase=f"tool_intent_{intent.recovery_policy}",
                safety=intent.recovery_policy,
            )
            db.commit()
            return self._intent_from_row(intent)

    def commit_recovered_result(
        self,
        effect_id: str,
        result: Any,
        *,
        recovery_owner: str,
    ) -> Any:
        value = self.commit_result(
            effect_id,
            run_owner=recovery_owner,
            claim_owner=recovery_owner,
            result=result,
            reconciled=True,
        )
        with self._sessions() as db:
            lease = (
                db.query(ToolEffectLease)
                .filter(ToolEffectLease.effect_id == effect_id)
                .with_for_update()
                .one()
            )
            run = (
                db.query(ChatRun)
                .filter(ChatRun.run_id == lease.run_id)
                .with_for_update()
                .one()
            )
            if lease.run_owner != recovery_owner or run.lease_owner != recovery_owner:
                raise ToolEffectLeaseLost(effect_id)
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = self._clock()
            db.commit()
        return value

    def release_recovery_claim(
        self,
        effect_id: str,
        *,
        recovery_owner: str,
        retry_seconds: int = 30,
    ) -> None:
        """Defer a failed adapter without letting RunJournal resume past the pending effect."""
        with self._sessions() as db:
            lease = (
                db.query(ToolEffectLease)
                .filter(ToolEffectLease.effect_id == effect_id)
                .with_for_update()
                .one()
            )
            if lease.run_owner != recovery_owner or lease.claim_owner != recovery_owner:
                raise ToolEffectLeaseLost(effect_id)
            run = (
                db.query(ChatRun)
                .filter(ChatRun.run_id == lease.run_id)
                .with_for_update()
                .one()
            )
            if run.lease_owner != recovery_owner:
                raise ToolEffectLeaseLost(effect_id)
            retry_at = self._clock() + timedelta(seconds=max(1, retry_seconds))
            lease.claim_owner = None
            lease.lease_expires_at = retry_at
            lease.updated_at = self._clock()
            run.lease_expires_at = retry_at
            run.updated_at = self._clock()
            db.commit()


class ToolEffectGateway:
    """The single execution state machine used by production middleware and tests."""

    def __init__(
        self,
        journal: ToolEffectJournal,
        registry: ToolRecoveryRegistry,
        *,
        poll_interval: float = 0.05,
        lease_seconds: int = 300,
    ) -> None:
        self.journal = journal
        self.registry = registry
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds

    @staticmethod
    async def _call(handler, intent: ToolIntent):  # noqa: ANN001, ANN205
        value = handler(intent) if handler is not None else None
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _reconciliation(value: Any) -> ReconciliationResult:
        if isinstance(value, ReconciliationResult):
            return value
        return (
            ReconciliationResult.applied(value)
            if value is not None
            else ReconciliationResult.unknown()
        )

    def _start_heartbeat(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
    ) -> tuple[asyncio.Task, asyncio.Event]:
        lost = asyncio.Event()
        interval = max(0.05, min(30.0, self.lease_seconds / 3))

        async def _beat() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    ok = await asyncio.to_thread(
                        self.journal.renew_invocation_claim,
                        effect_id,
                        run_owner=run_owner,
                        claim_owner=claim_owner,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:  # noqa: BLE001 - lost heartbeat means lost ownership
                    ok = False
                if not ok:
                    lost.set()
                    return

        return asyncio.create_task(_beat()), lost

    @staticmethod
    async def _stop_heartbeat(task: asyncio.Task) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def execute_outcome(
        self,
        *,
        run_id: str,
        owner: str,
        tool_call_id: str,
        tool_name: str,
        args: Mapping[str, Any],
        invoke: Callable[[], Awaitable[Any] | Any],
        idempotency_key: Optional[str] = None,
        classify_failure: Optional[Callable[[Any], bool]] = None,
    ) -> ToolExecutionOutcome:
        claim_owner = f"tool:{uuid.uuid4().hex}"
        policy = self.registry.resolve(tool_name)
        while True:
            decision = self.journal.begin_intent(
                run_id=run_id,
                owner=owner,
                claim_owner=claim_owner,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=args,
                recovery_policy=policy.policy,
                idempotency_key=idempotency_key,
                lease_seconds=self.lease_seconds,
            )
            if decision.action == "known":
                return ToolExecutionOutcome(
                    decision.result,
                    decision.intent.effect_id,
                    decision.intent.result_id,
                    decision.result_state,
                    False,
                )
            if decision.action == "unknown":
                raise ToolOutcomeUnknown(decision.effect_id)
            if decision.action == "wait":
                await asyncio.sleep(self.poll_interval)
                terminal = self.journal.terminal_decision(decision.effect_id)
                if terminal is None:
                    continue
                if terminal.action == "unknown":
                    raise ToolOutcomeUnknown(decision.effect_id)
                return ToolExecutionOutcome(
                    terminal.result,
                    terminal.intent.effect_id,
                    terminal.intent.result_id,
                    terminal.result_state,
                    False,
                )
            if decision.action not in ("execute", "reconcile"):
                raise ToolEffectError(
                    f"unsupported tool intent action: {decision.action}"
                )

            self.journal.acquire_invocation_claim(
                decision.effect_id,
                run_owner=owner,
                claim_owner=claim_owner,
                lease_seconds=self.lease_seconds,
            )
            heartbeat, claim_lost = self._start_heartbeat(
                decision.effect_id,
                run_owner=owner,
                claim_owner=claim_owner,
            )
            token = CURRENT_TOOL_EFFECT.set(
                ToolInvocationContext(
                    decision.intent.effect_id,
                    decision.intent.result_id,
                    decision.intent.idempotency_key,
                    decision.intent.run_id,
                    claim_owner,
                )
            )
            try:
                if decision.action == "reconcile":
                    if policy.reconciler is None:
                        self.journal.mark_unknown(
                            decision.effect_id,
                            owner=owner,
                            claim_owner=claim_owner,
                            reason="reconcile policy has no reconciliation adapter",
                        )
                        raise ToolOutcomeUnknown(decision.effect_id)
                    reconciliation = self._reconciliation(
                        await self._call(policy.reconciler, decision.intent)
                    )
                    if claim_lost.is_set():
                        raise ToolEffectLeaseLost(decision.effect_id)
                    if reconciliation.outcome == "applied":
                        result = self.journal.commit_result(
                            decision.effect_id,
                            run_owner=owner,
                            claim_owner=claim_owner,
                            result=reconciliation.result,
                            reconciled=True,
                        )
                        return ToolExecutionOutcome(
                            result,
                            decision.intent.effect_id,
                            decision.intent.result_id,
                            "success",
                            False,
                        )
                    if reconciliation.outcome != "not_applied":
                        self.journal.mark_unknown(
                            decision.effect_id,
                            owner=owner,
                            claim_owner=claim_owner,
                            reason="reconciliation could not determine external outcome",
                        )
                        raise ToolOutcomeUnknown(decision.effect_id)
                value = invoke()
                result = await value if inspect.isawaitable(value) else value
                if claim_lost.is_set():
                    raise ToolEffectLeaseLost(decision.effect_id)
                failed = (
                    bool(classify_failure(result))
                    if classify_failure is not None
                    else False
                )
                if failed and policy.policy != "replay_safe":
                    # AgentScope deliberately converts adapter exceptions into
                    # an ERROR ToolResponse. For side-effecting/non-replayable
                    # tools that response cannot prove whether the external
                    # effect happened, so fail closed and let the configured
                    # recovery policy reconcile or require manual review.
                    raise ToolOutcomeUnknown(decision.effect_id)
                result = self.journal.commit_result(
                    decision.effect_id,
                    run_owner=owner,
                    claim_owner=claim_owner,
                    result=result,
                    failed=failed,
                )
                return ToolExecutionOutcome(
                    result,
                    decision.intent.effect_id,
                    decision.intent.result_id,
                    "error" if failed else "success",
                    True,
                )
            except asyncio.CancelledError:
                with suppress(Exception):
                    self.journal.abandon_invocation_claim(
                        decision.effect_id,
                        run_owner=owner,
                        claim_owner=claim_owner,
                    )
                raise
            except ToolOutcomeUnknown:
                with suppress(Exception):
                    self.journal.abandon_invocation_claim(
                        decision.effect_id,
                        run_owner=owner,
                        claim_owner=claim_owner,
                    )
                raise
            except Exception as exc:
                with suppress(Exception):
                    self.journal.abandon_invocation_claim(
                        decision.effect_id,
                        run_owner=owner,
                        claim_owner=claim_owner,
                    )
                raise ToolOutcomeUnknown(decision.effect_id) from exc
            finally:
                await self._stop_heartbeat(heartbeat)
                CURRENT_TOOL_EFFECT.reset(token)

    async def execute(self, **kwargs) -> Any:  # noqa: ANN003
        return (await self.execute_outcome(**kwargs)).result


async def recover_incomplete_tool_effects(
    *,
    journal: Optional[ToolEffectJournal] = None,
    registry: Optional[ToolRecoveryRegistry] = None,
    lease_seconds: int = 120,
    heartbeat_interval: Optional[float] = None,
) -> list[RecoverySweepDecision]:
    """Recover each orphan independently; one broken adapter cannot starve later work."""

    journal = journal or ToolEffectJournal()
    registry = registry or DEFAULT_TOOL_RECOVERY_REGISTRY
    from core.harness.usage import (
        UsageAttempt,
        attempt_status_for_exception,
        record_usage_safely,
    )
    from core.services.harness_ledger import HarnessUsageLedger

    usage_ledger = HarnessUsageLedger(journal._sessions)

    async def _recorded_recovery_call(handler, intent, phase):  # noqa: ANN001
        started = time.monotonic()
        status = "failed"
        try:
            result = await ToolEffectGateway._call(handler, intent)
            status = "success"
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status = attempt_status_for_exception(exc)
            raise
        finally:
            try:
                await record_usage_safely(
                    usage_ledger,
                    UsageAttempt(
                        run_id=intent.run_id,
                        kind="tool",
                        operation_name=intent.tool_name,
                        effect_id=intent.effect_id,
                        status=status,
                        latency_ms=int((time.monotonic() - started) * 1_000),
                        metadata={"recovery_phase": phase},
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.debug("tool recovery usage persistence failed", exc_info=True)

    decisions: list[RecoverySweepDecision] = []
    for effect_id in journal.pending_effect_ids():
        terminal_intent = journal.settle_terminal_run_intent(effect_id)
        if terminal_intent is not None:
            decisions.append(
                RecoverySweepDecision(
                    effect_id, terminal_intent.run_id, "terminal_unknown"
                )
            )
            continue
        recovery_owner = f"tool-recovery:{uuid.uuid4().hex}"
        intent = journal.claim_recovery_intent(
            effect_id,
            recovery_owner=recovery_owner,
            lease_seconds=lease_seconds,
        )
        if intent is None:
            continue
        policy = registry.resolve(intent.tool_name)
        claim_lost = asyncio.Event()

        async def _heartbeat() -> None:
            interval = heartbeat_interval
            if interval is None:
                interval = max(0.05, min(30.0, lease_seconds / 3))
            while True:
                await asyncio.sleep(interval)
                try:
                    ok = await asyncio.to_thread(
                        journal.renew_recovery_claim,
                        effect_id,
                        recovery_owner=recovery_owner,
                        lease_seconds=lease_seconds,
                    )
                except Exception:  # noqa: BLE001 - lost heartbeat means lost ownership
                    ok = False
                if not ok:
                    claim_lost.set()
                    return

        heartbeat = asyncio.create_task(_heartbeat())
        try:
            if intent.recovery_policy == "never_replay":
                journal.mark_unknown(
                    effect_id,
                    owner=recovery_owner,
                    claim_owner=recovery_owner,
                    reason="non-replayable tool intent has no committed result",
                )
                decisions.append(
                    RecoverySweepDecision(effect_id, intent.run_id, "needs_attention")
                )
                continue
            if intent.recovery_policy == "replay_safe":
                if policy.replayer is None:
                    raise ToolEffectError("replay_safe policy has no replay adapter")
                result = await _recorded_recovery_call(
                    policy.replayer, intent, "replay"
                )
                if claim_lost.is_set():
                    raise ToolEffectLeaseLost(effect_id)
                journal.commit_recovered_result(
                    effect_id, result, recovery_owner=recovery_owner
                )
                decisions.append(
                    RecoverySweepDecision(effect_id, intent.run_id, "replayed")
                )
                continue

            if policy.reconciler is None:
                raise ToolEffectError("reconcile policy has no reconciliation adapter")
            reconciliation = ToolEffectGateway._reconciliation(
                await _recorded_recovery_call(policy.reconciler, intent, "reconcile")
            )
            if claim_lost.is_set():
                raise ToolEffectLeaseLost(effect_id)
            if reconciliation.outcome == "applied":
                result = reconciliation.result
            elif (
                reconciliation.outcome == "not_applied" and policy.replayer is not None
            ):
                result = await _recorded_recovery_call(
                    policy.replayer, intent, "replay_after_reconcile"
                )
            else:
                journal.mark_unknown(
                    effect_id,
                    owner=recovery_owner,
                    claim_owner=recovery_owner,
                    reason="reconciliation could not determine external outcome",
                )
                decisions.append(
                    RecoverySweepDecision(effect_id, intent.run_id, "needs_attention")
                )
                continue
            if claim_lost.is_set():
                raise ToolEffectLeaseLost(effect_id)
            journal.commit_recovered_result(
                effect_id, result, recovery_owner=recovery_owner
            )
            decisions.append(
                RecoverySweepDecision(effect_id, intent.run_id, "reconciled")
            )
        except Exception:  # noqa: BLE001 - release this claim and continue the sweep
            try:
                journal.release_recovery_claim(effect_id, recovery_owner=recovery_owner)
            except ToolEffectError:
                pass
            decisions.append(RecoverySweepDecision(effect_id, intent.run_id, "retry"))
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
    return decisions


__all__ = [
    "CURRENT_TOOL_EFFECT",
    "DEFAULT_TOOL_RECOVERY_REGISTRY",
    "IntentDecision",
    "ReconciliationResult",
    "RecoverySweepDecision",
    "ToolEffectError",
    "ToolEffectGateway",
    "ToolEffectJournal",
    "ToolEffectLeaseLost",
    "ToolExecutionOutcome",
    "ToolIdempotencyConflict",
    "ToolIntent",
    "ToolIntentCommitError",
    "ToolInvocationContext",
    "ToolOutcomeUnknown",
    "ToolPolicy",
    "ToolRecoveryRegistry",
    "build_default_tool_recovery_registry",
    "canonical_tool_args",
    "find_tool_outcome_unknown",
    "recover_incomplete_tool_effects",
    "stable_idempotency_key",
    "tool_args_hash",
]
