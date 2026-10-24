"""Database persistence and seed migration for system configuration."""
from __future__ import annotations
import logging
from typing import Optional
from core.db.engine import SessionLocal
from core.db.models import SystemConfig
from core.config.store import system_config_constants

logger = logging.getLogger(__name__)


class SystemConfigurationStore:
    def set(self, key: str, value: str | None, updated_by: str = "admin") -> None:
        """Update a config value in DB."""
        try:
            db = SessionLocal()
            try:
                row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
                if row is None:
                    logger.warning("[SystemConfigService] key %s not found, skipping set", key)
                    return
                row.config_value = value
                row.updated_by = updated_by
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.error("[SystemConfigService] set(%s) failed: %s", key, exc)
            raise
    def bulk_set(self, items: list[dict], updated_by: str = "admin") -> None:
        """Batch update multiple config values. Each item: {key, value}.

        For secret fields, masked values (containing '****') are skipped to
        prevent the frontend from accidentally overwriting real secrets with
        their masked representation.
        """
        try:
            db = SessionLocal()
            try:
                for item in items:
                    key = item.get("key", "").strip()
                    value = item.get("value")
                    if not key:
                        continue
                    row = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
                    if row is None:
                        continue
                    # Skip masked values for secret fields
                    if row.is_secret and isinstance(value, str) and "****" in value:
                        continue
                    row.config_value = value if value != "" else None
                    row.updated_by = updated_by
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.error("[SystemConfigService] bulk_set failed: %s", exc)
            raise
    def _ensure_seed_rows(self, db) -> None:
        """Insert any missing seed config rows. Idempotent."""
        rows_by_key = {row.config_key: row for row in db.query(SystemConfig).all()}
        existing_keys = set(rows_by_key)
        inserted_keys: set[str] = set()
        inserted = 0
        for key, val, display, desc, group, secret in system_config_constants.SEED_CONFIGS:
            if key not in existing_keys:
                row = SystemConfig(
                    config_key=key,
                    config_value=val,
                    display_name=display,
                    description=desc,
                    group_key=group,
                    is_secret=secret,
                )
                db.add(row)
                rows_by_key[key] = row
                inserted_keys.add(key)
                inserted += 1
        migrated = self._migrate_legacy_knowledge_config(rows_by_key, inserted_keys)
        if inserted or migrated:
            db.commit()
            logger.info(
                "[SystemConfigService] Inserted %d missing seed row(s), migrated %d legacy KB value(s)",
                inserted,
                migrated,
            )
    @staticmethod
    def _migrate_legacy_knowledge_config(
        rows_by_key: dict[str, SystemConfig],
        inserted_keys: set[str],
    ) -> int:
        """Move the old shared KB connection into one provider exactly once.

        Older releases stored every external backend in the shared
        knowledge_base.url/api_key/allowed_dataset_ids rows. Those rows cannot
        prove which dropdown option they belonged to, and copying them into the
        currently selected provider can reproduce the original cross-provider
        leak. Treat them as Dify compatibility values and leave FastGPT/WeKnora
        empty until each provider is configured explicitly.

        inserted_keys is the one-time guard: after scoped rows exist, later
        legacy edits can never overwrite provider-specific values.
        """
        migrated = 0
        for field in system_config_constants.PROVIDER_SCOPED_KNOWLEDGE_FIELDS:
            destination_key = f"knowledge_base.dify.{field}"
            if destination_key not in inserted_keys:
                continue
            destination = rows_by_key.get(destination_key)
            legacy = rows_by_key.get(f"knowledge_base.{field}")
            legacy_value = getattr(legacy, "config_value", None)
            if (
                destination is not None
                and not getattr(destination, "config_value", None)
                and legacy_value not in (None, "")
            ):
                destination.config_value = legacy_value
                migrated += 1
        return migrated
    def load(self):
        new_cache: dict[str, Optional[str]] = {}
        new_meta: dict[str, dict] = {}
        try:
            db = SessionLocal()
            try:
                self._ensure_seed_rows(db)
                rows = db.query(SystemConfig).all()
                for row in rows:
                    new_cache[row.config_key] = row.config_value
                    new_meta[row.config_key] = {
                        "config_key": row.config_key,
                        "config_value": row.config_value,
                        "display_name": row.display_name,
                        "description": row.description,
                        "group_key": row.group_key,
                        "is_secret": row.is_secret,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                        "updated_by": row.updated_by,
                    }
            finally:
                db.close()
        except Exception as exc:
            logger.warning("[SystemConfigService] DB load failed, keeping stale cache: %s", exc)
            raise

        return new_cache, new_meta
