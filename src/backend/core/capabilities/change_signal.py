"""能力变更号：让桌面本机端不靠轮询也能发现云端的能力增删。

桌面双端的本机后端只在两类事件下同步云端能力：**登录**，以及**智能体 / 技能 /
连接器 / 插件被新增或删除**。后者就是这里的信号——变更号随每个 HTTP 响应头下发，
桌面端发现它变了才提示用户同步。没有定时器，也不产生额外请求。

信号刻意只认「增删」：改内容、启停、以及其它缓存失效都不触发，否则同步会退化成
变相轮询。

变更号是**从数据本身算出来的**（四张表的行数与最新一条的创建时间），不是进程内的
计数器。这一点是必须的：线上后端跑多个 worker，各自计数会让同一时刻的两个进程给出
不同的值，客户端轮流打到不同进程就会不停看到「变了」——实测生产上两个 worker 的
计数分别停在 9 和 13，客户端一天因此触发上千次同步。指纹由数据决定，哪个进程算出来
的都一样。

行数只在新增/删除时变；最新创建时间覆盖「删一条又加一条」这种行数不变的情况。改内容
和启停都不影响这两个量。
"""

from __future__ import annotations

import hashlib
import threading
import time

# 指纹的缓存时长。进程自己做的增删会立刻失效缓存，这个窗口只决定「别的 worker 改了
# 之后多久被看到」，与用户感知的同步延迟同阶；代价是每个进程每 10 秒四条计数查询。
_TTL_SECONDS = 10.0

_lock = threading.Lock()
_value = ""
_expires = 0.0


def _capability_models():
    from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin, UserAgent

    return (AdminSkill, UserAgent, AdminMcpServer, InstalledPlugin)


def _fingerprint() -> str:
    """四类能力当前集合的指纹；读不到数据库时返回空串，调用方保持上一次的值。"""
    from sqlalchemy import func, select

    from core.db.engine import SessionLocal

    parts: list[str] = []
    try:
        with SessionLocal() as db:
            for model in _capability_models():
                total = db.execute(select(func.count()).select_from(model.__table__)).scalar_one()
                newest = db.execute(select(func.max(model.created_at))).scalar()
                parts.append(f"{model.__tablename__}:{total}:{newest or ''}")
    except Exception:  # noqa: BLE001 - 信号取不到不能影响请求本身
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def invalidate() -> None:
    """让下一次读取重新计算（本进程刚做过增删时调用）。"""
    global _expires
    with _lock:
        _expires = 0.0


def bump() -> str:
    """有一条能力被新增或删除了。"""
    invalidate()
    return current()


def current() -> str:
    global _value, _expires
    now = time.monotonic()
    with _lock:
        if _value and now < _expires:
            return _value
    fresh = _fingerprint()
    with _lock:
        if fresh:
            _value = fresh
            _expires = now + _TTL_SECONDS
        return _value


def watch_capability_tables(engine) -> None:
    """四类能力的记录被插入 / 删除时让指纹立刻重算（幂等，启动时调一次）。

    判定挂在数据层——这四类记录所在的表发生 INSERT / DELETE 就算一次增删，所以任何
    新写的增删路径都自动被覆盖，不需要在每个路由里补打点。
    """
    from sqlalchemy import event
    from sqlalchemy.sql.expression import Delete, Insert

    tables = {model.__tablename__ for model in _capability_models()}

    @event.listens_for(engine, "after_execute")
    def _bump_on_capability_change(conn, clauseelement, multiparams, params, execution_options, result):  # noqa: ANN001
        if not isinstance(clauseelement, (Insert, Delete)):
            return
        table = getattr(clauseelement, "table", None)
        if table is not None and table.name in tables:
            invalidate()
