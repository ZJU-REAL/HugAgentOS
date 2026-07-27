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
Real isolation is the OS-level sandbox (macOS seatbelt / Windows Job Object /
Linux landlock) layered on top (tickets #09 / #11 / #12). Keep this gate as
defense-in-depth, never as the only line.

EDITION / WEB-SAFETY
--------------------
Edition-agnostic: lives in shared ``core`` so it is present in both the cloud
(EE) tree and the desktop CE derivation. The module is **inert** until wired
into the bash tool upstream, and that wiring is gated on local mode — so the
cloud / web deployment is unaffected.

The decision vocabulary intentionally matches the existing human-in-the-loop
(HITL) semantics: ``confirm`` maps onto the ``tool_pending`` / ``file_confirm``
approval flow, ``deny`` blocks outright, ``allow`` runs unattended.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

# ── Decision vocabulary ──────────────────────────────────────────────────────
ALLOW = "allow"
DENY = "deny"
CONFIRM = "confirm"

# Severity ordering — the most restrictive triggered rule wins.
_SEVERITY = {ALLOW: 0, CONFIRM: 1, DENY: 2}

# ── Danger categories ────────────────────────────────────────────────────────
DELETE = "delete"
SYSTEM_WRITE = "system_write"
NETWORK = "network"
PRIVILEGE = "privilege"

# Policy dispositions (what the user configures per category) → decision.
_DISPOSITION_TO_DECISION = {"block": DENY, "confirm": CONFIRM, "allow": ALLOW}


@dataclass(frozen=True)
class Grant:
    """A user-authorized local directory.

    ``mode`` is ``"read"`` or ``"readwrite"``. A read-only grant lets the agent
    read inside the directory but a write-intent command targeting it is treated
    as out-of-scope.
    """

    path: str
    mode: str = "readwrite"


@dataclass(frozen=True)
class Policy:
    """User-configurable local-execution policy.

    ``out_of_scope``: disposition when a command targets a path outside the
    workspace root and outside every grant. One of ``block`` / ``confirm`` /
    ``allow``.

    ``danger``: per-category disposition. Missing categories fall back to the
    built-in defaults below.
    """

    out_of_scope: str = "confirm"
    danger: dict = field(default_factory=dict)

    def disposition_for(self, category: str) -> str:
        return self.danger.get(category, _DEFAULT_DANGER[category])


# Sane defaults. System writes and privilege escalation are hard-blocked by
# default; deletes and network egress ask for confirmation. Deployments may
# override every entry from the settings panel (ticket #07 AC).
_DEFAULT_DANGER = {
    DELETE: "confirm",
    SYSTEM_WRITE: "block",
    NETWORK: "confirm",
    PRIVILEGE: "block",
}


@dataclass
class EvalResult:
    """Outcome of :func:`evaluate_local_command`.

    ``decision`` is the string the caller acts on. ``reasons`` is a list of
    human-readable triggers for the audit log (ticket #07 AC: judgements are
    recorded).
    """

    decision: str
    reasons: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # allow `if result:` truthiness on allow
        return self.decision == ALLOW


# ── Danger-command tables (per platform) ─────────────────────────────────────
# Each entry: (category, compiled regex over the *raw* command string). Regexes
# are deliberately broad — false positives degrade to a confirm prompt, which is
# the safe direction. Platform-specific tables (ticket #10 keeps the Windows
# table in sync with the POSIX one).
_POSIX_DANGER = [
    (DELETE, re.compile(r"\brm\s+(-\w*\s+)*-\w*[rf]", re.I)),
    (DELETE, re.compile(r"\b(rmdir|unlink|shred)\b", re.I)),
    (DELETE, re.compile(r"\bmkfs\b|\bdd\b.*\bof=/dev/", re.I)),
    (SYSTEM_WRITE, re.compile(r">>?\s*/(etc|usr|bin|sbin|boot|sys|System|Library)\b", re.I)),
    (SYSTEM_WRITE, re.compile(r"\b(chmod|chown)\b.*\s/(etc|usr|bin|sbin|System)\b", re.I)),
    (NETWORK, re.compile(r"\b(curl|wget|nc|ncat|scp|sftp|ssh|telnet)\b", re.I)),
    (NETWORK, re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b", re.I)),
    (PRIVILEGE, re.compile(r"\b(sudo|doas|su)\b", re.I)),
]

_WINDOWS_DANGER = [
    (DELETE, re.compile(r"\b(del|erase)\b.*/[sf]\b", re.I)),
    (DELETE, re.compile(r"\b(rd|rmdir)\b.*/s\b", re.I)),
    (DELETE, re.compile(r"\bformat\b|\bRemove-Item\b.*-Recurse", re.I)),
    (SYSTEM_WRITE, re.compile(r"[A-Za-z]:\\(Windows|Program Files)\b", re.I)),
    (SYSTEM_WRITE, re.compile(r"\breg\s+(add|delete)\b.*HKLM", re.I)),
    (NETWORK, re.compile(r"\b(Invoke-WebRequest|iwr|curl|wget|certutil|bitsadmin)\b", re.I)),
    (PRIVILEGE, re.compile(r"\brunas\b|-Verb\s+RunAs", re.I)),
]


def _pathmod(platform: str):
    return ntpath if platform == "windows" else posixpath


def _norm(path: str, platform: str) -> str:
    pm = _pathmod(platform)
    norm = pm.normpath(path)
    if platform == "windows":
        norm = norm.replace("/", "\\").casefold()
    return norm.rstrip(pm.sep) or pm.sep


def _is_under(path: str, base: str, platform: str) -> bool:
    """True when ``path`` equals ``base`` or is nested inside it."""
    pm = _pathmod(platform)
    p = _norm(path, platform)
    b = _norm(base, platform)
    return p == b or p.startswith(b + pm.sep)


def _looks_like_path(token: str, platform: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token.startswith("~") or "/" in token:
        return True
    if platform == "windows":
        # backslash path or drive-letter absolute
        return "\\" in token or bool(re.match(r"^[A-Za-z]:", token))
    return False


def _resolve(token: str, cwd: str, platform: str) -> str:
    pm = _pathmod(platform)
    t = token
    if t.startswith("~"):
        # Home cannot be resolved portably in a pure function; treat as absolute
        # sentinel so it is judged out-of-scope unless a grant covers it.
        return _norm(t, platform)
    if pm.isabs(t):
        return _norm(t, platform)
    return _norm(pm.join(cwd, t), platform)


def _extract_target_paths(command: str, cwd: str, platform: str) -> List[str]:
    try:
        tokens = shlex.split(command, posix=(platform != "windows"))
    except ValueError:
        # Unbalanced quotes etc. — fall back to whitespace split so we still scan.
        tokens = command.split()
    out: List[str] = []
    for tok in tokens:
        # strip redirection prefixes like >file or >>file
        stripped = tok.lstrip("<>")
        if _looks_like_path(stripped, platform):
            out.append(_resolve(stripped, cwd, platform))
    return out


def _in_scope(path: str, workspace_root: str, grants: List[Grant], platform: str) -> bool:
    if _is_under(path, workspace_root, platform):
        return True
    for g in grants:
        if _is_under(path, g.path, platform):
            return True
    return False


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

    # Axis 1 — path scope.
    for path in _extract_target_paths(command, cwd, platform):
        if not _in_scope(path, workspace_root, grants, platform):
            d = _DISPOSITION_TO_DECISION[policy.out_of_scope]
            decisions.append(d)
            reasons.append(f"out-of-scope path: {path}")

    # Axis 2 — command risk.
    table = _WINDOWS_DANGER if platform == "windows" else _POSIX_DANGER
    seen_categories = set()
    for category, pattern in table:
        if category in seen_categories:
            continue
        if pattern.search(command):
            seen_categories.add(category)
            d = _DISPOSITION_TO_DECISION[policy.disposition_for(category)]
            decisions.append(d)
            reasons.append(f"danger:{category}")

    decision = max(decisions, key=lambda d: _SEVERITY[d])
    return EvalResult(decision=decision, reasons=reasons)


__all__ = [
    "ALLOW",
    "DENY",
    "CONFIRM",
    "DELETE",
    "SYSTEM_WRITE",
    "NETWORK",
    "PRIVILEGE",
    "Grant",
    "Policy",
    "EvalResult",
    "evaluate_local_command",
]
