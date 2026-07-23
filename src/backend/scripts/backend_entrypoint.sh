#!/usr/bin/env bash

set -euo pipefail

MAX_RETRIES="${DB_MIGRATION_RETRIES:-20}"
SLEEP_SECONDS="${DB_MIGRATION_RETRY_INTERVAL:-2}"

echo "[entrypoint] Starting backend bootstrap..."

# Detect whether the database is SQLite.  Alembic migration files use
# PostgreSQL-specific DDL (JSONB, GIN indexes, partial indexes …) which
# cannot run on SQLite.  For SQLite we fall back to SQLAlchemy
# ``Base.metadata.create_all()`` which respects the dialect-aware type
# variants defined in the ORM models.
DB_URL="${DATABASE_URL:-sqlite:///./hugagent.db}"
if echo "${DB_URL}" | grep -qi "^sqlite"; then
  echo "[entrypoint] SQLite detected – using create_all() instead of alembic..."
  python -c "from core.db.engine import init_db; init_db()"
  echo "[entrypoint] SQLite tables created successfully."
else
  echo "[entrypoint] Running database migrations (alembic upgrade head)..."
  attempt=1
  until alembic upgrade head; do
    if [ "${attempt}" -ge "${MAX_RETRIES}" ]; then
      echo "[entrypoint] Migration failed after ${attempt} attempts."
      exit 1
    fi
    echo "[entrypoint] Migration attempt ${attempt} failed, retrying in ${SLEEP_SECONDS}s..."
    attempt=$((attempt + 1))
    sleep "${SLEEP_SECONDS}"
  done
  echo "[entrypoint] Migrations applied successfully."
  if [ "${JX_EDITION:-ee}" = "ce" ]; then
    echo "[entrypoint] Reconciling the CE schema against the current metadata..."
    python -c "from core.db.engine import init_db; init_db()"
    echo "[entrypoint] CE schema reconciliation completed."
  fi
fi

echo "[entrypoint] Starting API server..."

exec uvicorn api.app:app --host 0.0.0.0 --port "${PORT:-3001}"
