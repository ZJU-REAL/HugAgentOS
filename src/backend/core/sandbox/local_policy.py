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
Strong write isolation is the OS-level sandbox (macOS Seatbelt / Linux
bubblewrap) layered on top. Unsupported platforms fail closed in restricted
modes. Keep this command classifier as defense-in-depth, never as the only line.

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
import os
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import List, Optional

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
    # Canonical host paths that this command is expected to modify.  The bash
    # wrapper uses these only after the policy decision has allowed/confirmed
    # the command, to build a one-shot OS-sandbox writable set.  Read operands
    # (for example the source of ``cp``) must never appear here.
    write_paths: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # allow `if result:` truthiness on allow
        return self.decision == ALLOW


# ── Danger-command tables (per platform) ─────────────────────────────────────
# Each entry: (category, compiled regex over the *raw* command string). Regexes
# are deliberately broad — false positives degrade to a confirm prompt, which is
# the safe direction. Platform-specific tables (ticket #10 keeps the Windows
# table in sync with the POSIX one).
#
# Bare command names must only match in *command position*. A plain ``\brm\b``
# also fires on ``docker run --rm``, ``npm rm`` and any argument that happens to
# contain the word — turning routine commands into confirmation prompts and
# training users to click through them.

# Start of a command: line start, a shell separator, a subshell opener, or one
# of the dispatchers that take a command as their argument.
_CMD_START = r"(?:^|[\n;&|(`]+|\$\(|(?:-execdir|-exec)\s+)\s*"
# Wrappers that precede the real command without changing its identity.
_CMD_WRAPPERS = r"(?:(?:sudo|doas|env|command|nohup|time|xargs)\s+(?:-\S+\s+)*)*"


def _command_re(names: str) -> "re.Pattern[str]":
    """Compile a danger pattern that only matches ``names`` in command position.

    The trailing guard is stricter than ``\\b``: ``ssh-keygen`` and ``rd-tool``
    are different programs from ``ssh`` and ``rd``, and a word boundary alone
    would classify them as the dangerous one.
    """
    return re.compile(_CMD_START + _CMD_WRAPPERS + r"(?:\S*/)?(?:" + names + r")(?![\w.-])", re.I)


_POSIX_DANGER = [
    (DELETE, _command_re(r"rm|rmdir|unlink|shred")),
    # Destructive flag shape, wherever it appears (``find … -exec rm -rf`` and
    # friends), so position anchoring can never weaken the obvious case.
    (DELETE, re.compile(r"\brm\s+(-\w*\s+)*-\w*[rf]", re.I)),
    (DELETE, re.compile(r"\bmkfs\b|\bdd\b.*\bof=/dev/", re.I)),
    (SYSTEM_WRITE, re.compile(r">>?\s*/(etc|usr|bin|sbin|boot|sys|System|Library)\b", re.I)),
    (SYSTEM_WRITE, re.compile(r"\b(chmod|chown)\b.*\s/(etc|usr|bin|sbin|System)\b", re.I)),
    (NETWORK, _command_re(r"curl|wget|nc|ncat|scp|sftp|ssh|telnet")),
    (NETWORK, re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b", re.I)),
    (PRIVILEGE, _command_re(r"sudo|doas|su")),
]

_WINDOWS_DANGER = [
    (DELETE, _command_re(r"del|erase|rd|rmdir|Remove-Item")),
    (DELETE, re.compile(r"\bformat\b|\bRemove-Item\b.*-Recurse", re.I)),
    (SYSTEM_WRITE, re.compile(r"[A-Za-z]:\\(Windows|Program Files)\b", re.I)),
    (SYSTEM_WRITE, re.compile(r"\breg\s+(add|delete)\b.*HKLM", re.I)),
    (NETWORK, _command_re(r"Invoke-WebRequest|iwr|curl|wget|certutil|bitsadmin")),
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


def _canonical(path: str, platform: str) -> str:
    """Resolve path identity before doing containment checks.

    On the current host this resolves symlinks (including an existing symlink
    parent of a not-yet-created file). Cross-platform unit-test inputs retain
    their native lexical semantics instead of being interpreted by the host OS.
    """
    expanded = os.path.expanduser(path)
    actual_windows = os.name == "nt"
    if (platform == "windows") == actual_windows:
        expanded = os.path.realpath(os.path.abspath(expanded))
    return _norm(expanded, platform)


def _is_under(path: str, base: str, platform: str) -> bool:
    """True when ``path`` equals ``base`` or is nested inside it."""
    pm = _pathmod(platform)
    p = _norm(path, platform)
    b = _norm(base, platform)
    if platform != "windows" and b == pm.sep:
        return p.startswith(pm.sep)
    return p == b or p.startswith(b + pm.sep)


def _looks_like_path(token: str, platform: str) -> bool:
    if not token or token.startswith("-"):
        return False
    # URLs are command arguments, not host paths.  Treating them as relative
    # paths produces misleading scope decisions for curl/wget.
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", token):
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
        return _canonical(t, platform)
    if pm.isabs(t):
        return _canonical(t, platform)
    return _canonical(pm.join(cwd, t), platform)


def _shell_tokens(command: str, platform: str) -> List[str]:
    try:
        if platform == "windows":
            return shlex.split(command, posix=False)
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        # Unbalanced quotes etc. — fall back to whitespace split so we still scan.
        return command.split()


def _command_segments(command: str, platform: str) -> List[List[str]]:
    """Tokenize shell control-flow without splitting quoted separators."""
    segments: List[List[str]] = [[]]
    for token in _shell_tokens(command, platform):
        # Redirection groups contain ``<``/``>`` and stay in the command.  Pure
        # control groups delimit commands/pipeline stages.
        if token and set(token) <= {";", "&", "|"}:
            if segments[-1]:
                segments.append([])
            continue
        segments[-1].append(token)
    return [segment for segment in segments if segment]


def _extract_target_paths(command: str, cwd: str, platform: str) -> List[str]:
    out: List[str] = []
    for tok in _shell_tokens(command, platform):
        # strip redirection prefixes like >file or >>file
        stripped = tok.lstrip("<>")
        if _looks_like_path(stripped, platform):
            out.append(_resolve(stripped, cwd, platform))
    return out


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

# Exact output-redirection operators (``>``, ``>>``, ``2>``, ``&>``, ``>&``) and
# the attached-target form (``>out.txt``) produced by the whitespace fallback
# tokenizer and by Windows non-POSIX splitting.
_REDIRECT_OP_RE = re.compile(r"^(?:\d*|&)(?:>>?|>&)$")
_REDIRECT_ATTACHED_RE = re.compile(r"^(?:\d*|&)>>?(?P<target>[^>].*)$")


def _extract_write_target_paths(command: str, cwd: str, platform: str) -> List[str]:
    """Best-effort extraction of explicit filesystem mutation targets.

    This is deliberately narrower than :func:`_has_write_intent`: unknown or
    indirect write-shaped commands remain confined by the OS sandbox, while
    only targets clearly visible in the command can receive a one-shot writable
    bind after approval.
    """
    out: List[str] = []

    def add(token: str) -> None:
        candidate = token.lstrip("<>")
        if _looks_like_path(candidate, platform):
            resolved = _resolve(candidate, cwd, platform)
            if resolved not in out:
                out.append(resolved)

    for segment in _command_segments(command, platform):
        # Shell output redirections are always write targets. Only an *exact*
        # redirection operator counts: a token that merely contains ``>``
        # (``"a->b"``, a quoted ``x>y`` comparison) is an ordinary argument, and
        # promoting its neighbour would both mis-classify a read operand as a
        # write and hand that path a real writable bind after approval.
        for index, token in enumerate(segment):
            if _REDIRECT_OP_RE.match(token):
                if index + 1 < len(segment):
                    add(segment[index + 1])
                continue
            attached = _REDIRECT_ATTACHED_RE.match(token)
            if attached:
                add(attached.group("target"))
                continue
            if token.casefold().startswith("of="):
                add(token[3:])

        # Find the executable while tolerating common wrappers and assignments.
        command_index = None
        for index, token in enumerate(segment):
            lowered = token.casefold()
            if lowered in {"sudo", "doas", "env", "command", "nohup"}:
                continue
            if "=" in token and not token.startswith(("/", "\\")):
                continue
            if token.startswith("-") or token in {"<", ">", ">>", "<<", "<<<"}:
                continue
            command_index = index
            break
        if command_index is None:
            continue

        executable = segment[command_index].replace("\\", "/").rsplit("/", 1)[-1].casefold()
        args = segment[command_index + 1 :]
        path_args = [token for token in args if _looks_like_path(token, platform)]

        if executable in _WRITE_DESTINATION_ONLY and path_args:
            # GNU cp/install support ``-t DIR``; otherwise the final path is the
            # destination and preceding paths are read-only sources.
            if "-t" in args and args.index("-t") + 1 < len(args):
                add(args[args.index("-t") + 1])
            elif "--target-directory" in args and args.index("--target-directory") + 1 < len(args):
                add(args[args.index("--target-directory") + 1])
            else:
                add(path_args[-1])
        elif executable in _WRITE_SOURCE_AND_DESTINATION:
            # Moving/renaming also removes or changes the source entry.
            for token in path_args:
                add(token)
        elif executable in _WRITE_ALL_OPERANDS:
            for token in path_args:
                add(token)
            # PowerShell write cmdlets commonly spell the target after -Path.
            for flag in ("-Path", "-LiteralPath", "-OutFile"):
                if flag in args and args.index(flag) + 1 < len(args):
                    add(args[args.index(flag) + 1])
        elif executable == "sed" and any(re.match(r"^-[A-Za-z]*i", arg) for arg in args):
            for token in path_args:
                add(token)
        elif executable == "patch":
            for flag in ("-d", "--directory"):
                if flag in args and args.index(flag) + 1 < len(args):
                    add(args[args.index(flag) + 1])
        elif executable == "git":
            write_subcommands = {
                "add",
                "apply",
                "checkout",
                "switch",
                "restore",
                "reset",
                "clean",
                "commit",
                "merge",
                "rebase",
                "cherry-pick",
                "revert",
                "stash",
                "pull",
            }
            if any(arg.casefold() in write_subcommands for arg in args):
                for flag in ("-C", "--git-dir", "--work-tree"):
                    if flag in args and args.index(flag) + 1 < len(args):
                        add(args[args.index(flag) + 1])

    return out


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
    decisions.append(scope_decision)
    return EvalResult(decision=max(decisions, key=lambda d: _SEVERITY[d]), reasons=reasons)


_POSIX_WRITE_INTENT = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+|env\s+)*"
    r"(?:rm|rmdir|unlink|shred|touch|mkdir|mktemp|cp|mv|install|ln|chmod|chown|"
    r"truncate|tee|patch|make|cmake|ninja|npm|pnpm|yarn|pip|uv|cargo)\b|"
    r"\bsed\s+[^;&|]*-[A-Za-z]*i\b|"
    r"\bgit\s+(?:add|apply|checkout|switch|restore|reset|clean|commit|merge|rebase|"
    r"cherry-pick|revert|stash|pull)\b|"
    r"(?:^|[^<])>>?\s*[^&]",
    re.I,
)

_WINDOWS_WRITE_INTENT = re.compile(
    r"\b(?:del|erase|rd|rmdir|copy|move|mkdir|md|ren|rename)\b|"
    r"\btype\b[^\r\n]*>\s*|"
    r"\b(?:Remove-Item|Set-Content|Add-Content|Out-File|New-Item|Copy-Item|Move-Item|"
    r"Rename-Item|Set-ItemProperty|New-ItemProperty)\b|"
    r"(?:^|[^<])>>?\s*[^&]",
    re.I,
)


def _has_write_intent(command: str, platform: str) -> bool:
    pattern = _WINDOWS_WRITE_INTENT if platform == "windows" else _POSIX_WRITE_INTENT
    return bool(pattern.search(command))


def _system_write_roots(platform: str) -> tuple[str, ...]:
    return (
        (r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)")
        if platform == "windows"
        else (
            "/etc",
            "/usr",
            "/bin",
            "/sbin",
            "/boot",
            "/sys",
            "/lib",
            "/lib64",
            "/System",
            "/Library",
        )
    )


def _is_system_write_path(path: str, platform: str) -> bool:
    roots = _system_write_roots(platform)
    return any(_is_under(path, root, platform) for root in roots)


def intersects_system_write_area(path: str, platform: str = "posix") -> bool:
    """Whether a writable root overlaps a protected system directory."""
    resolved = _canonical(path, platform)
    return any(
        _is_under(resolved, root, platform) or _is_under(root, resolved, platform)
        for root in _system_write_roots(platform)
    )


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
    for path in _extract_target_paths(command, cwd, platform):
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


__all__ = [
    "ALLOW",
    "DENY",
    "CONFIRM",
    "READ",
    "WRITE",
    "DELETE",
    "SYSTEM_WRITE",
    "NETWORK",
    "PRIVILEGE",
    "Grant",
    "Policy",
    "EvalResult",
    "intersects_system_write_area",
    "evaluate_local_path",
    "evaluate_local_command",
]
