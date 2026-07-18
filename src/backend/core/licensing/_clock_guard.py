"""Clock-rollback protection for offline licenses (commercial-edition only — excluded from the CE derived tree).

Offline license expiry checks depend on the system clock; a customer could
bypass expiry simply by setting the system time back. This module maintains a
high-water mark of the "largest date ever observed": every time today's date is
requested it returns ``max(system today, high-water mark)``, and persists the
new maximum whenever the date advances. Once the clock is rolled back, the
effective date stays at the high-water mark and never regresses, so expiry
checks cannot be bypassed by rewinding the clock.

The high-water mark is persisted in two places (the effective value is the max
of both plus the system clock), so wiping/tampering with either one does not
defeat the guard:

- DB: a ``content_blocks`` row (id=:data:`_HW_BLOCK_ID`) — volume-persisted,
  survives rebuilds
- File: a sidecar next to the license file (``<license>.seen``) — fallback when
  the DB gets reset

An additional in-process cache of the high-water mark avoids hitting the DB/disk
on every request — the two stores are only written back when the date advances
(roughly once per day).

> Honest disclaimer: an attacker with full control of the host + DB can still
> bypass this by wiping both stores and rolling back the clock at the same
> time; that is the nature of offline licensing (the machine is in the
> customer's hands). This guard blocks casual bypasses like "just change the
> system time to extend the license", raising the bar to "must tamper with
> multiple pieces of persisted state simultaneously".
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Id of the high-water-mark row in content_blocks. Underscore prefix +
# non-business semantics, to avoid confusion with operational content.
_HW_BLOCK_ID = "_license_clock_hw"

# In-process high-water-mark cache (seeded once from storage on cold start).
_hw_cache: Optional[date] = None
_seeded = False


def _sidecar_path(license_path: str) -> Path:
    return Path(license_path + ".seen")


def _parse(value: object) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


# ── sidecar file ──────────────────────────────────────────────────────────

def _read_sidecar(license_path: str) -> Optional[date]:
    try:
        return _parse(_sidecar_path(license_path).read_text(encoding="utf-8"))
    except OSError:
        return None


def _write_sidecar(license_path: str, d: date) -> None:
    try:
        p = _sidecar_path(license_path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(d.isoformat(), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:  # Write failure is non-fatal: DB still backs us up, retry next time
        logger.warning("[license] 时钟高水位 sidecar 写入失败: %s", e)


# ── DB (content_blocks, best-effort, never raises) ─────────────────────────

def _read_db() -> Optional[date]:
    try:
        from core.db.engine import SessionLocal
        from core.db.models.artifact import ContentBlock

        with SessionLocal() as db:
            row = db.get(ContentBlock, _HW_BLOCK_ID)
            return _parse(row.payload) if row else None
    except Exception:  # noqa: BLE001 - silently degrade to sidecar when DB is unavailable / table missing
        return None


def _write_db(d: date) -> None:
    try:
        from core.db.engine import SessionLocal
        from core.db.models.artifact import ContentBlock

        with SessionLocal() as db:
            row = db.get(ContentBlock, _HW_BLOCK_ID)
            if row is None:
                db.add(ContentBlock(id=_HW_BLOCK_ID, payload=d.isoformat(),
                                    updated_by="license_clock_guard"))
            else:
                row.payload = d.isoformat()
            db.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[license] 时钟高水位 DB 写入失败: %s", e)


# ── public API ──────────────────────────────────────────────────────────────

def monotonic_today(license_path: Optional[str]) -> date:
    """Return a monotonically non-decreasing "today": ``max(system today, largest date ever observed)``.

    When no license path is configured there is no expiry semantics to guard,
    so return the system date directly (no storage reads/writes).
    """
    global _hw_cache, _seeded

    today = date.today()
    if not license_path:
        return today

    if not _seeded:
        # Cold start: seed the high-water mark from both stores (each best-effort).
        candidates = [d for d in (_read_sidecar(license_path), _read_db()) if d]
        _hw_cache = max(candidates) if candidates else None
        _seeded = True

    effective = today if _hw_cache is None else max(today, _hw_cache)
    if _hw_cache is None or effective > _hw_cache:
        _hw_cache = effective
        _write_sidecar(license_path, effective)
        _write_db(effective)
    return effective


def reset_cache() -> None:
    """Clear the in-process cache (used by tests and license hot-reload)."""
    global _hw_cache, _seeded
    _hw_cache = None
    _seeded = False
