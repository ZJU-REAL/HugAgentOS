"""Sensitive-data sanitization gate.

Inlines a default set of hardcoded rules covering the mixed government + enterprise scenarios
HugAgentOS currently serves:
- Generic PII (national ID, mobile number, email, bank card)
- Secrets (API key, JWT)
- Government (official red-header document numbers)
- Enterprise (customer IDs, intranet URLs)

The DB table `memory_sanitizer_rules` supports appending / disabling specific rules at runtime
(5-minute TTL cache).

Hit on CLASSIFIED_TERMS → **write rejected** (returns text=None, reject=True).
Hit on REDACT_PATTERNS → **replaced with [REDACTED:<name>] but still written**.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Optional, Pattern

logger = logging.getLogger(__name__)


# ─── Default hardcoded rules ────────────────────────────────────────────────

REDACT_PATTERNS: dict[str, Pattern[str]] = {
    # Generic PII
    "id_card":      re.compile(r"(?<!\d)[1-9]\d{14}(?:\d{2}[\dXx])?(?!\d)"),
    "phone_cn":     re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "email":        re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+"),
    "bank_card":    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    # Secrets
    "api_key":      re.compile(r"(?:sk-|Bearer\s+)[A-Za-z0-9_\-]{20,}"),
    "jwt":          re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # Government
    "doc_no":       re.compile(r"[\u4e00-\u9fa5]{2,4}〔\d{4}〕\d+号"),
    "gov_serial":   re.compile(r"[\u4e00-\u9fa5]{2,8}[字发]〔\d{4}〕\d+"),
    # Enterprise
    "customer_id":  re.compile(r"(?:CUST|CID)[-_]?\d{4,}"),
    "internal_url": re.compile(r"https?://[\w.-]*\.(?:internal|corp|intra)\b"),
}


CLASSIFIED_TERMS: tuple[str, ...] = (
    "机密",
    "秘密",
    "绝密",
    "内部资料",
    "内部文件",
    "Confidential",
    "Restricted",
    "NDA",
    "内部限阅",
)


# ─── Data structures ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SanitizeResult:
    """Sanitization result.

    - text:  the sanitized text; None when reject=True
    - hits:  list of matched rule names (e.g. ["id_card", "phone_cn"] or ["classified:机密"])
    - reject: True means CLASSIFIED_TERMS was triggered; writing is forbidden
    """

    text: Optional[str]
    hits: list[str] = field(default_factory=list)
    reject: bool = False

    @property
    def clean(self) -> bool:
        """Neither matched any redaction rule nor triggered a write rejection."""
        return not self.hits and not self.reject


# ─── DB rule overrides (with TTL cache) ──────────────────────────────────────


_RULES_CACHE_TTL = 300  # seconds
_rules_cache: dict = {"patterns": None, "classified": None, "loaded_at": 0.0}
_rules_lock = Lock()


def invalidate_rules_cache() -> None:
    """Called proactively after an admin adds/removes rules; the next sanitize re-pulls from the DB."""
    with _rules_lock:
        _rules_cache["patterns"] = None
        _rules_cache["classified"] = None
        _rules_cache["loaded_at"] = 0.0


def _load_rules_with_db_overrides() -> tuple[dict[str, Pattern[str]], tuple[str, ...]]:
    """Merge default rules + DB dynamic rules, returning (patterns, classified_terms).

    DB rule fields, see `core/db/models.py::MemorySanitizerRule`:
    - rule_type: "redact" | "classified" | "disable_redact" | "disable_classified"
    - pattern: a redact regex or a classified term
    - name: the redact rule name

    On DB query failure, falls back to the hardcoded rules without affecting the main flow.
    """
    now = time.time()
    with _rules_lock:
        if (
            _rules_cache["patterns"] is not None
            and now - _rules_cache["loaded_at"] < _RULES_CACHE_TTL
        ):
            return _rules_cache["patterns"], _rules_cache["classified"]

    patterns = dict(REDACT_PATTERNS)
    classified = list(CLASSIFIED_TERMS)

    try:
        from core.db.engine import SessionLocal
        from core.db.models import MemorySanitizerRule

        with SessionLocal() as session:
            rules = session.query(MemorySanitizerRule).filter(MemorySanitizerRule.enabled == True).all()  # noqa: E712
            for r in rules:
                if r.rule_type == "redact":
                    try:
                        patterns[r.name or f"db_{r.id}"] = re.compile(r.pattern)
                    except re.error as exc:
                        logger.warning("[sanitizer] invalid db redact pattern id=%s: %s", r.id, exc)
                elif r.rule_type == "classified":
                    classified.append(r.pattern)
                elif r.rule_type == "disable_redact" and r.name in patterns:
                    patterns.pop(r.name, None)
                elif r.rule_type == "disable_classified" and r.pattern in classified:
                    classified.remove(r.pattern)
    except Exception as exc:
        # DB table missing (migration not run) or connection failure → silent fallback
        logger.debug("[sanitizer] DB overrides unavailable, using hardcoded only: %s", exc)

    result_classified = tuple(classified)
    with _rules_lock:
        _rules_cache["patterns"] = patterns
        _rules_cache["classified"] = result_classified
        _rules_cache["loaded_at"] = now
    return patterns, result_classified


# ─── Main function ──────────────────────────────────────────────────────────


def sanitize(text: str) -> SanitizeResult:
    """Filter sensitive data from memory content about to be stored.

    1. Hit on CLASSIFIED_TERMS → return reject=True immediately, skipping the replacement stage
    2. Hit on REDACT_PATTERNS → replaced with [REDACTED:<name>], still written
    3. Clean text → returned unchanged

    Callers need not special-case empty strings; empty string / None yields
    `SanitizeResult(text="", clean=True)`.
    """
    if text is None:
        return SanitizeResult(text="", hits=[], reject=False)

    patterns, classified_terms = _load_rules_with_db_overrides()

    for term in classified_terms:
        if term in text:
            return SanitizeResult(text=None, hits=[f"classified:{term}"], reject=True)

    hits: list[str] = []
    cleaned = text
    for name, pat in patterns.items():
        if pat.search(cleaned):
            hits.append(name)
            cleaned = pat.sub(f"[REDACTED:{name}]", cleaned)
    return SanitizeResult(text=cleaned, hits=hits, reject=False)
