"""Safety helpers for ontology-generated revision candidates."""

from __future__ import annotations

import re


_SUBSTANTIVE_CHARACTER_RE = re.compile(r"[A-Za-z0-9\u3400-\u9fff]")
_REVISION_OPEN_TAG = "<ontology_revision>"
_REVISION_CLOSE_TAG = "</ontology_revision>"


def normalize_revision_candidate(value: object) -> str:
    """Return candidate body text without ontology transport wrappers.

    The wrapper belongs to the repair-stream protocol, not to the answer that
    is displayed or persisted.  Keep this cleanup at the domain boundary as a
    final safeguard even though the streaming parser normally removes it.
    """

    text = str(value or "").strip()
    if text.startswith(_REVISION_OPEN_TAG):
        text = text[len(_REVISION_OPEN_TAG) :].lstrip()
    if text.endswith(_REVISION_CLOSE_TAG):
        text = text[: -len(_REVISION_CLOSE_TAG)].rstrip()
    return text.strip()


def is_substantive_revision(value: object, *, minimum_characters: int = 8) -> bool:
    """Return whether *value* contains enough real text to replace an answer.

    A non-empty check is not sufficient here: some reasoning models mention the
    required wrapper as ``<ontology_revision>...</ontology_revision>`` before
    producing the real body.  The ellipsis must never become a persisted or
    user-acceptable replacement candidate.
    """

    text = normalize_revision_candidate(value)
    if not text:
        return False
    return len(_SUBSTANTIVE_CHARACTER_RE.findall(text)) >= minimum_characters
