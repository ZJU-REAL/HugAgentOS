"""Local-mode execution policy gate (desktop only, host-subprocess sandbox).

Ticket #07 decision core. A **pure function** that classifies a shell command
about to run in the no-Docker host-subprocess sandbox as ``allow`` / ``deny`` /
``confirm``, given the user's authorized folders (grants) and their
danger-command policy.

SECURITY NOTE
-------------
This is a *string/argument-level heuristic*. It prevents fat-finger mistakes and
produces an audit trail, but it is **not** a strong isolation boundary — a
crafted shell command (variable indirection, base64, sub-shells) can evade it.
The real boundary is the OS sandbox in :mod:`core.sandbox.oslayer` (macOS
Seatbelt, Linux bubblewrap, Windows low-integrity token), which every preset except
the unrestricted one runs under and which refuses the command outright when it
cannot be applied. Keep this command classifier as defense-in-depth, never as
the only line.

EDITION / WEB-SAFETY
--------------------
Edition-agnostic: lives in shared ``core`` so it is present in both the cloud
(EE) tree and the desktop CE derivation. The module is **inert** until wired
into the bash tool upstream, and that wiring is gated on local mode — so the
cloud / web deployment is unaffected.

The decision vocabulary intentionally matches the existing human-in-the-loop
(HITL) semantics: ``confirm`` maps onto the ``tool_pending`` / ``file_confirm``
approval flow, ``deny`` blocks outright, ``allow`` runs unattended."""

from __future__ import annotations

from typing import List, Optional

from core.sandbox.permissions.models import (
    _DISPOSITION_TO_DECISION,
    _SEVERITY,
    ALLOW,
    DANGER_REASON_PREFIX,
    OUT_OF_SCOPE_WRITE,
    READ,
    SYSTEM_WRITE,
    WRITE,
    EvalResult,
    Grant,
    Policy,
)
from core.sandbox.permissions.paths import _canonical, _is_system_write_path, _is_under
from core.sandbox.permissions.shell import (
    _POSIX_DANGER,
    _WINDOWS_DANGER,
    _extract_target_paths,
    _extract_write_target_paths,
    _has_write_intent,
)


def evaluate_local_path(
    path: str,
    *,
    intent: str,
    grants: Optional[List[Grant]] = None,
    policy: Optional[Policy] = None,
    workspace_root: str = "/workspace",
    platform: str = "posix",
) -> EvalResult:
    """Classify one direct host-path access by canonical identity and intent."""
    if intent not in (READ, WRITE):
        raise ValueError(f"unsupported local path intent: {intent}")
    grants = grants or []
    policy = policy or Policy()
    resolved = _canonical(path, platform)
    workspace = _canonical(workspace_root, platform)
    decisions = [ALLOW]
    reasons: List[str] = []

    # Strict/read-only is a global property, not merely a workspace rule.  A
    # standing read-write Grant must not punch a write hole through this preset.
    if intent == WRITE:
        write_decision = _DISPOSITION_TO_DECISION[policy.workspace_write]
        decisions.append(write_decision)
        if write_decision != ALLOW:
            reasons.append(f"write intent: {resolved}")
        if _is_system_write_path(resolved, platform):
            system_decision = _DISPOSITION_TO_DECISION[policy.disposition_for(SYSTEM_WRITE)]
            decisions.append(system_decision)
            if system_decision != ALLOW:
                reasons.append(f"danger:{SYSTEM_WRITE}")

    if _is_under(resolved, workspace, platform):
        scope_decision = ALLOW
    else:
        matching_grants = [
            grant
            for grant in grants
            if _is_under(resolved, _canonical(grant.path, platform), platform)
        ]
        if matching_grants and (
            intent == READ or any(grant.mode == "readwrite" for grant in matching_grants)
        ):
            scope_decision = ALLOW
        else:
            scope_decision = _DISPOSITION_TO_DECISION[policy.out_of_scope]
            if scope_decision != ALLOW:
                reason = "read-only grant write" if matching_grants else "out-of-scope path"
                reasons.append(f"{reason}: {resolved}")
                if intent == WRITE:
                    # 处置仍然是 policy.out_of_scope；这里只是标出「这不是一次普通写入」，
                    # 好让「替我批准」停下来问一句，而不是当成日常操作直接放过去。
                    reasons.append(f"{DANGER_REASON_PREFIX}{OUT_OF_SCOPE_WRITE}")
    decisions.append(scope_decision)
    return EvalResult(decision=max(decisions, key=lambda d: _SEVERITY[d]), reasons=reasons)


def evaluate_local_command(
    command: str,
    cwd: str,
    grants: Optional[List[Grant]] = None,
    policy: Optional[Policy] = None,
    *,
    workspace_root: str = "/workspace",
    platform: str = "posix",
) -> EvalResult:
    """Classify a shell command for the local host-subprocess sandbox.

    Returns an :class:`EvalResult` whose ``decision`` is ``allow`` / ``deny`` /
    ``confirm``. The most restrictive triggered rule wins across two axes:

    1. **Path scope** — any target path outside ``workspace_root`` and outside
       every grant triggers ``policy.out_of_scope``.
    2. **Command risk** — danger-category matches trigger their configured
       disposition, regardless of path (a delete inside an authorized folder
       still confirms by default).

    A command that triggers nothing is ``allow``.
    """
    grants = grants or []
    policy = policy or Policy()
    reasons: List[str] = []
    decisions: List[str] = [ALLOW]
    write_intent = _has_write_intent(command, platform)

    # A strict/read-only preset blocks write-shaped commands even when they use
    # only relative paths (which the lightweight path scanner may not extract).
    if write_intent:
        d = _DISPOSITION_TO_DECISION[policy.workspace_write]
        decisions.append(d)
        if d != ALLOW:
            reasons.append("workspace write intent")

    # Axis 1 — path scope.
    write_paths = _extract_write_target_paths(command, cwd, platform)
    write_path_set = set(write_paths)
    for path in dict.fromkeys([*_extract_target_paths(command, cwd, platform), *write_paths]):
        path_result = evaluate_local_path(
            path,
            intent=WRITE if path in write_path_set else READ,
            grants=grants,
            policy=policy,
            workspace_root=workspace_root,
            platform=platform,
        )
        decisions.append(path_result.decision)
        reasons.extend(path_result.reasons)

    # Structured destination classification closes regex-shape gaps such as
    # ``cp file /etc/service.conf``. Reads of those paths remain governed only
    # by path scope; the system-write category applies strictly to mutations.
    system_write_detected = any(_is_system_write_path(path, platform) for path in write_paths)
    if system_write_detected:
        d = _DISPOSITION_TO_DECISION[policy.disposition_for(SYSTEM_WRITE)]
        decisions.append(d)
        reasons.append(f"danger:{SYSTEM_WRITE}")

    # Axis 2 — command risk.
    table = _WINDOWS_DANGER if platform == "windows" else _POSIX_DANGER
    seen_categories = {SYSTEM_WRITE} if system_write_detected else set()
    for category, pattern in table:
        if category in seen_categories:
            continue
        if pattern.search(command):
            seen_categories.add(category)
            d = _DISPOSITION_TO_DECISION[policy.disposition_for(category)]
            decisions.append(d)
            reasons.append(f"danger:{category}")

    decision = max(decisions, key=lambda d: _SEVERITY[d])
    return EvalResult(
        decision=decision,
        reasons=list(dict.fromkeys(reasons)),
        write_paths=write_paths,
    )
