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

import re
import shlex
from typing import List

from core.sandbox.permissions.models import (
    _WRITE_ALL_OPERANDS,
    _WRITE_DESTINATION_ONLY,
    _WRITE_SOURCE_AND_DESTINATION,
    CREDENTIAL,
    DELETE,
    NETWORK,
    PRIVILEGE,
    SYSTEM_WRITE,
)
from core.sandbox.permissions.paths import _looks_like_path, _resolve

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
    # macOS keychain / freedesktop secret service / kernel keyring: the same
    # stores the desktop client itself keeps the session token in.
    (CREDENTIAL, _command_re(r"security|secret-tool|keyctl|gnome-keyring(-daemon)?")),
]

_WINDOWS_DANGER = [
    (DELETE, _command_re(r"del|erase|rd|rmdir|Remove-Item")),
    (DELETE, re.compile(r"\bformat\b|\bRemove-Item\b.*-Recurse", re.I)),
    (SYSTEM_WRITE, re.compile(r"[A-Za-z]:\\(Windows|Program Files)\b", re.I)),
    (SYSTEM_WRITE, re.compile(r"\breg\s+(add|delete)\b.*HKLM", re.I)),
    (NETWORK, _command_re(r"Invoke-WebRequest|iwr|curl|wget|certutil|bitsadmin")),
    (PRIVILEGE, re.compile(r"\brunas\b|-Verb\s+RunAs", re.I)),
    (CREDENTIAL, _command_re(r"cmdkey|vaultcmd")),
    (CREDENTIAL, re.compile(r"\b(Get-Credential|Get-StoredCredential|PasswordVault)\b", re.I)),
]


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

    def add(token: str, *, explicit_target: bool = False) -> None:
        candidate = token.lstrip("<>")
        # An explicit redirection also names a path when it is only a basename.
        # Descriptor duplication and unresolved shell variables are not literals.
        literal_basename = (
            explicit_target and bool(candidate) and not candidate.startswith(("$", "&"))
        )
        if _looks_like_path(candidate, platform) or literal_basename:
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
                    target = segment[index + 1]
                    if token.endswith(">&") and target.isdecimal():
                        continue
                    add(target, explicit_target=True)
                continue
            attached = _REDIRECT_ATTACHED_RE.match(token)
            if attached:
                add(attached.group("target"), explicit_target=True)
                continue
            if token.casefold().startswith("of="):
                add(token[3:], explicit_target=True)

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
