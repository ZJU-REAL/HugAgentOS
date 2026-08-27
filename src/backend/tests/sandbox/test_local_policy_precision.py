"""Precision of the local command classifier.

Over-broad matching is not a safe default here. Every spurious ``confirm``
trains the user to click through prompts, and a spurious *write target* is
worse than noise: after approval it becomes a real writable bind in the OS
sandbox. These tests pin both directions — the dangerous shapes still fire,
the ordinary ones no longer do.
"""

from __future__ import annotations

import pytest
from core.sandbox.local_policy import (
    _POSIX_DANGER,
    _WINDOWS_DANGER,
    DELETE,
    NETWORK,
    _extract_write_target_paths,
)


def _categories(command: str, platform: str = "posix") -> set[str]:
    table = _WINDOWS_DANGER if platform == "windows" else _POSIX_DANGER
    return {category for category, pattern in table if pattern.search(command)}


# ── Danger detection only in command position ────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/build",
        "sudo rm x",
        "cd /tmp && rm note.txt",
        "echo hi | rm y",
        "find . -name '*.pyc' -exec rm {} \\;",
        "xargs -0 rm < list.txt",
        "/bin/rm data.bin",
        "$(rm secret)",
    ],
)
def test_real_deletions_are_still_flagged(command):
    assert DELETE in _categories(command)


@pytest.mark.parametrize(
    "command",
    [
        "docker run --rm -it ubuntu bash",
        "docker compose run --rm web pytest",
        "npm rm left-pad",
        "echo 'rm is dangerous'",
        "ls -la",
        "cat report.txt",
    ],
)
def test_the_word_rm_outside_command_position_is_not_a_deletion(command):
    """``--rm`` is a container flag, ``npm rm`` is a package operation."""
    assert DELETE not in _categories(command)


@pytest.mark.parametrize(
    "command,expected",
    [("ssh user@host", True), ("ssh-keygen -t ed25519", False), ("curl https://x", True)],
)
def test_similarly_named_programs_are_distinguished(command, expected):
    """``ssh-keygen`` is a local key generator, not a network client."""
    assert (NETWORK in _categories(command)) is expected


@pytest.mark.parametrize(
    "command",
    ["del /f build.log", "cd tmp & rd /s out", "Remove-Item -Recurse .\\dist"],
)
def test_windows_deletions_are_still_flagged(command):
    assert DELETE in _categories(command, platform="windows")


@pytest.mark.parametrize("command", ["docker run --rm app", "echo del"])
def test_windows_deletion_words_outside_command_position_are_ignored(command):
    assert DELETE not in _categories(command, platform="windows")


# ── Write-target extraction: only real redirection operators ─────────────────


@pytest.mark.parametrize(
    "command,expected",
    [
        ("echo hi > /home/u/notes/new.txt", ["/home/u/notes/new.txt"]),
        ("cmd 2>/tmp/err.log", ["/tmp/err.log"]),
        ("echo x >>./log.txt", ["/workspace/log.txt"]),
        ("cp a.txt /etc/svc.conf", ["/etc/svc.conf"]),
    ],
)
def test_genuine_write_targets_are_extracted(command, expected):
    assert _extract_write_target_paths(command, "/workspace", "posix") == expected


@pytest.mark.parametrize(
    "command",
    [
        'echo "a->b" ./out.txt',
        'python -c "print(1 if a>b else 2)" ./data.txt',
        "grep -r foo ./src",
        "cat ./config.yaml",
    ],
)
def test_a_quoted_angle_bracket_does_not_fabricate_a_write_target(command):
    """A token that merely contains ``>`` is an argument, not a redirection.

    Promoting its neighbour would both misclassify a read operand as a write
    and hand that path a writable bind once the command is approved.
    """
    assert _extract_write_target_paths(command, "/workspace", "posix") == []
