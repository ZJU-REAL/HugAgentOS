"""chat state responsibilities."""
from __future__ import annotations
from sqlalchemy import JSON, BigInteger, Integer, String
from sqlalchemy.dialects.postgresql import INET, JSONB

JSONType = JSON().with_variant(JSONB(), "postgresql")


INETType = String(45).with_variant(INET(), "postgresql")


BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
