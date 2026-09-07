"""能力变更号：让桌面本机端不靠轮询也能发现云端的能力增删。

桌面双端的本机后端只在两类事件下同步云端能力：**登录**，以及**智能体 / 技能 /
连接器 / 插件被新增或删除**。后者就是这里的信号——变更号随每个 HTTP 响应头下发，
桌面端发现它变了才调一次本机 ``/v1/desktop/capabilities/sync``。没有定时器，也不
产生额外请求。

信号刻意只认「增删」：改内容、启停、以及其它缓存失效都不触发，否则同步会退化成
变相轮询。判定挂在数据层——这四类记录所在的表发生 INSERT / DELETE 就算一次增删，
所以任何新写的增删路径都自动被覆盖，不需要在每个路由里补打点。

进程重启会换一个更大的起始值——单调不回退，最多多同步一次。
"""

from __future__ import annotations

import itertools
import threading
import time

_boot = int(time.time())
_counter = itertools.count(1)
_lock = threading.Lock()
_value = f"{_boot}-0"


def bump() -> str:
    """有一条能力被新增或删除了。"""
    global _value
    with _lock:
        _value = f"{_boot}-{next(_counter)}"
        return _value


def current() -> str:
    return _value


def watch_capability_tables(engine) -> None:
    """四类能力的记录被插入 / 删除时递增变更号（幂等，启动时调一次）。"""
    from sqlalchemy import event
    from sqlalchemy.sql.expression import Delete, Insert

    from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin, UserAgent

    tables = {
        model.__tablename__
        for model in (AdminSkill, UserAgent, AdminMcpServer, InstalledPlugin)
    }

    @event.listens_for(engine, "after_execute")
    def _bump_on_capability_change(conn, clauseelement, multiparams, params, execution_options, result):  # noqa: ANN001
        if not isinstance(clauseelement, (Insert, Delete)):
            return
        table = getattr(clauseelement, "table", None)
        if table is not None and table.name in tables:
            bump()
