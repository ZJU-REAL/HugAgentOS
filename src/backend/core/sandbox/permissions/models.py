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

from dataclasses import dataclass, field
from typing import List, Sequence, Set

# ── Decision vocabulary ──────────────────────────────────────────────────────
ALLOW = "allow"

DENY = "deny"

CONFIRM = "confirm"

READ = "read"

WRITE = "write"

# Severity ordering — the most restrictive triggered rule wins.
_SEVERITY = {ALLOW: 0, CONFIRM: 1, DENY: 2}

# ── Danger categories ────────────────────────────────────────────────────────
DELETE = "delete"

SYSTEM_WRITE = "system_write"

NETWORK = "network"

PRIVILEGE = "privilege"

# Reading the operating system's own credential store. An agent never needs it,
# and one such command hands over the desktop session token in plain text — the
# rest of the danger table is about damage, this one is about impersonation.
CREDENTIAL = "credential"

# Writing to a path outside the workspace and outside every granted folder.
#
# Not a configurable category: its disposition **is** ``Policy.out_of_scope``.
# It exists so the layer above can tell "ordinary write" from "write somewhere
# the user never authorized" — 「替我批准」is meant to wave through the former,
# and it used to wave through the latter too because nothing marked it.
OUT_OF_SCOPE_WRITE = "out_of_scope_write"

# A triggered danger category is recorded in ``EvalResult.reasons`` under this
# prefix. Callers that need to know *why* a command needs confirming read it
# back through ``danger_categories`` instead of matching the prose.
DANGER_REASON_PREFIX = "danger:"


def danger_categories(reasons: Sequence[str]) -> Set[str]:
    """Danger categories behind a verdict, recovered from its reasons."""
    return {
        reason[len(DANGER_REASON_PREFIX) :]
        for reason in reasons
        if reason.startswith(DANGER_REASON_PREFIX)
    }


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

    ``workspace_write``: global write disposition (named for backward-compatible
    preset storage). ``strict`` sets it to ``block`` so neither the workspace nor
    an otherwise read-write Grant can be modified.

    ``danger``: per-category disposition. Missing categories fall back to the
    built-in defaults below.
    """

    out_of_scope: str = "confirm"
    workspace_write: str = "allow"
    danger: dict = field(default_factory=dict)

    def disposition_for(self, category: str) -> str:
        return self.danger.get(category, _DEFAULT_DANGER.get(category, CONFIRM))


# Sane defaults. System writes and privilege escalation are hard-blocked by
# default; deletes and network egress ask for confirmation. Deployments may
# override every entry from the settings panel (ticket #07 AC).
_DEFAULT_DANGER = {
    DELETE: "confirm",
    SYSTEM_WRITE: "block",
    NETWORK: "confirm",
    PRIVILEGE: "block",
    CREDENTIAL: "block",
}

# The configurable danger vocabulary, derived from the defaults above so the
# settings panel, its validation and the "allow everything" preset cannot drift
# apart from the classifier. Adding a category here is enough for all of them.
DANGER_CATEGORIES = tuple(_DEFAULT_DANGER)


@dataclass
class EvalResult:
    """Outcome of :func:`evaluate_local_command`.

    ``decision`` is the string the caller acts on. ``reasons`` is a list of
    human-readable triggers for the audit log (ticket #07 AC: judgements are
    recorded).
    """

    decision: str
    reasons: List[str] = field(default_factory=list)
    # Canonical host paths that this command is expected to modify.  The bash
    # wrapper uses these only after the policy decision has allowed/confirmed
    # the command, to build a one-shot OS-sandbox writable set.  Read operands
    # (for example the source of ``cp``) must never appear here.
    write_paths: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # allow `if result:` truthiness on allow
        return self.decision == ALLOW


_WRITE_ALL_OPERANDS = {
    "rm",
    "rmdir",
    "unlink",
    "shred",
    "touch",
    "mkdir",
    "chmod",
    "chown",
    "truncate",
    "tee",
    "remove-item",
    "set-content",
    "add-content",
    "out-file",
    "new-item",
    "rename-item",
    "set-itemproperty",
    "new-itemproperty",
    "del",
    "erase",
    "rd",
    "md",
}

_WRITE_DESTINATION_ONLY = {"cp", "copy", "install", "ln", "copy-item"}

_WRITE_SOURCE_AND_DESTINATION = {"mv", "move", "move-item", "ren", "rename"}
