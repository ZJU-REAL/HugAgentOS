"""Portable column types shared by every ORM model: JSONB/INET on PostgreSQL, plain types elsewhere."""
from __future__ import annotations
from sqlalchemy import JSON, BigInteger, Integer, String
from sqlalchemy.dialects.postgresql import INET, JSONB

JSONType = JSON().with_variant(JSONB(), "postgresql")
INETType = String(45).with_variant(INET(), "postgresql")
# SQLite only auto-increments the INTEGER rowid, so a BIGINT key would stay NULL there.
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
