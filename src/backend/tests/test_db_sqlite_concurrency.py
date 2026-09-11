"""SQLite 并发：缺省的回滚日志模式下，一个还开着的读事务就能把写请求逼到
``database is locked``。

首次能力同步（123 项）里失败 4 项就是这个形状：能力准备在多个线程里写库，同时
启动 seeding、能力视图重建、记忆初始化等还在读同一个文件。WAL 下读写互不阻塞，
只有写与写排队。
"""

import sqlite3
import threading
from pathlib import Path

from core.db.engine import apply_sqlite_concurrency_pragmas
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

# 两种配置用同样短的等待预算：差别只剩日志模式，避免把结论混到超时长短上。
WAIT_BUDGET_SECONDS = 1


class _Probe(Base):
    __tablename__ = "concurrency_probe"

    id = Column(Integer, primary_key=True)
    value = Column(String, default="")


def _write_while_a_reader_is_open(path: Path, *, pragmas: bool) -> Exception | None:
    """一个连接开着读事务，另一个线程写；返回写线程遇到的异常。"""
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": WAIT_BUDGET_SECONDS},
    )
    if pragmas:
        apply_sqlite_concurrency_pragmas(engine, WAIT_BUDGET_SECONDS)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    seed = session_factory()
    seed.add(_Probe(id=1, value=""))
    seed.commit()
    seed.close()

    reader = engine.raw_connection()
    cursor = reader.cursor()
    # pysqlite 只在写语句前才隐式开事务，读锁要显式拿。
    cursor.execute("BEGIN")
    cursor.execute("SELECT id, value FROM concurrency_probe").fetchall()

    outcome: dict[str, Exception] = {}

    def write() -> None:
        session = session_factory()
        try:
            row = session.get(_Probe, 1)
            row.value = "writer"
            session.commit()
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            session.close()

    thread = threading.Thread(target=write)
    thread.start()
    thread.join(30)

    cursor.close()
    reader.rollback()
    reader.close()
    engine.dispose()
    return outcome.get("error")


def test_default_journal_mode_lets_a_reader_block_writes(tmp_path: Path) -> None:
    error = _write_while_a_reader_is_open(tmp_path / "default.db", pragmas=False)

    assert error is not None, "缺省配置下读事务本应挡住写，测试没有复现出竞争"
    assert "database is locked" in str(error)


def test_configured_connection_writes_while_a_reader_is_open(tmp_path: Path) -> None:
    error = _write_while_a_reader_is_open(tmp_path / "configured.db", pragmas=True)

    assert error is None, f"WAL 下读事务不应再挡住写：{error}"


def test_configured_connection_reports_wal_and_the_shared_wait_budget(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'pragmas.db'}")
    apply_sqlite_concurrency_pragmas(engine, 30)

    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        assert cursor.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert cursor.execute("PRAGMA busy_timeout").fetchone()[0] == 30_000
        cursor.close()
    finally:
        connection.close()
        engine.dispose()


def test_memory_database_still_connects(tmp_path: Path) -> None:
    """内存库不落盘，journal_mode 对它无意义；设置不得因此报错。"""
    engine = create_engine("sqlite://")
    apply_sqlite_concurrency_pragmas(engine, 30)

    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        assert cursor.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
        cursor.close()
    finally:
        connection.close()
        engine.dispose()


def test_sqlite3_module_default_is_a_five_second_budget() -> None:
    """记录本次判断依据：等待预算并非 0，而是 pysqlite 给的 5 秒。"""
    connection = sqlite3.connect(":memory:")
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
    finally:
        connection.close()
