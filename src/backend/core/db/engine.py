"""Database configuration and session management."""

import logging
import os
from typing import Generator

from core.config.settings import settings
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

logger = logging.getLogger(__name__)


def _resolve_database_url() -> str:
    """Resolve database URL with a dev-safe fallback."""
    url = settings.db.url
    if url.startswith("postgresql://"):
        try:
            import psycopg2  # type: ignore # noqa: F401
        except ModuleNotFoundError:
            fallback = settings.db.sqlite_fallback_url
            logger.warning(
                "psycopg2 is not installed, fallback to SQLite for local run. fallback=%s",
                fallback,
            )
            return fallback
    return url


# Database URL from environment variable
DATABASE_URL = _resolve_database_url()

engine_kwargs = {
    "pool_pre_ping": True,
    "echo": settings.db.echo,
}

if DATABASE_URL.startswith("sqlite://"):
    # SQLite-specific options for local development/testing.
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    # Streaming requests can hold connections for a long time. NullPool avoids
    # exhausting a small QueuePool in local SQLite dev mode.
    if (
        os.name == "nt"
        and settings.deploy.is_local
        and make_url(DATABASE_URL).database not in (None, "", ":memory:")
    ):
        # Retain a few SQLite page/schema caches instead of reopening the file
        # for every capability lookup. Unlimited overflow keeps the old NullPool
        # behavior for long streams: no request waits for a free connection.
        engine_kwargs.update(poolclass=QueuePool, pool_size=8, max_overflow=-1, pool_use_lifo=True)
    else:
        engine_kwargs["poolclass"] = NullPool
else:
    engine_kwargs["pool_size"] = settings.db.pool_size
    engine_kwargs["max_overflow"] = settings.db.pool_max_overflow
    engine_kwargs["pool_timeout"] = settings.db.pool_timeout
    # 等锁必须有上界：无上界的行锁等待会把持有它的工作线程（或误写成 async 的路由的
    # 事件循环）无限期占住，另一侧又要等这条请求收尾才能释放锁，形成互相干等的死结。
    # 等锁与等连接同属「等数据库资源」，共用 DB_POOL_TIMEOUT 一个阈值。
    if make_url(DATABASE_URL).get_backend_name() == "postgresql":
        engine_kwargs["connect_args"] = {
            "options": f"-c lock_timeout={settings.db.pool_timeout * 1000}"
        }


def apply_sqlite_concurrency_pragmas(target, busy_timeout_seconds: int) -> None:
    """让 SQLite 能承受多线程并发访问。

    * ``journal_mode=WAL``：缺省的回滚日志模式下读写互斥——**一个还开着的读事务就
      能把写请求逼到** ``database is locked``。本机后端里能力准备在多个线程写库，
      同时启动 seeding、能力视图重建、记忆初始化还在读同一个文件，于是首次能力
      同步 123 项里有 4 项直接失败。WAL 下读不再挡写，只有写与写需要排队。
    * ``busy_timeout``：排队要等多久。pysqlite 自带 5 秒，对上面这种成批写入偏紧。
      等锁与等连接同属「等数据库资源」，沿用 ``DB_POOL_TIMEOUT`` 这一个阈值——与
      Postgres 分支拿它做 ``lock_timeout`` 是同一套口径，不新增开关。

    内存库不落盘，``journal_mode`` 对它无意义（SQLite 保持 ``memory``），设置无害，
    因此不为它分叉。
    """

    @event.listens_for(target, "connect")
    def _set_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_seconds * 1000)}")
        finally:
            cursor.close()


# Create engine
engine = create_engine(DATABASE_URL, **engine_kwargs)

if DATABASE_URL.startswith("sqlite://"):
    apply_sqlite_concurrency_pragmas(engine, settings.db.pool_timeout)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """
    Dependency function to get database session.

    Usage:
        @app.get("/items")
        def get_items(db: Session = Depends(get_db)):
            return db.query(Item).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize or reconcile the database schema for the active edition."""
    if settings.edition.edition == "ce":
        if settings.deploy.is_local:
            from core.db.local_schema_upgrade import reconcile_local_chat_sequences

            local_report = reconcile_local_chat_sequences(engine)
            if any(local_report.values()):
                logger.info("Local database compatibility schema reconciled: %s", local_report)

        from core.db.edition_tables import ce_reconcile_schema

        report = ce_reconcile_schema(engine)
        if any(report.values()):
            logger.info("CE database schema reconciled: %s", report)
        if settings.deploy.is_local:
            # 桌面本机单机库：再补一次全量 create_all，把共享树 metadata 里的全部表
            # 建齐。EE 风格树（如 HugAgentOS）在 CE 运行时下仍会触达团队等 EE 查询
            # 路径——空表让其自然返回空结果，而不是 sqlite "no such table" 500。
            # 完整版的 core.db.models 门面会注册 EE 模型；CE overlay 则只导出
            # 社区模型。共享引擎无需、也不得直接依赖 edition_ee 包。
            from core.db import models  # noqa: F401

            Base.metadata.create_all(bind=engine)
        return
    # Import models so SQLAlchemy metadata is populated before create_all().
    from core.db import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
