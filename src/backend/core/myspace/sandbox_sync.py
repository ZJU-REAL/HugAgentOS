"""沙箱的 ``/myspace`` 不在本机时，把它的现状搬进镜像目录。

opensandbox 开着 myspace bind mount 时（多用户部署与生产用的就是这套），沙箱写的
``/workspace/myspace/{uid}`` 就是后端的 ``myspace_cache/{uid}`` 本身，本模块什么都不做 ——
:mod:`core.space_sync.personal` 直接从文件事件看到一切。

script_runner 和 cube 不是这个拓扑：前者的会话工作区在共享容器内、一个会话一份，后者整个
沙箱在远端。两种情况下本机都没有可监听的目录，唯一的办法是把沙箱里的现状取回来。

取回来之后就没有第二条登记路径了：文件落进镜像目录，剩下的判定（新文件 / 改了用户已有
文件 / 用户已删的残留）、确认、登记、软删，全部由监听器按同一套判据完成。所以本模块只搬
运，不判断、不登记、不问用户 —— 它是登记器的一个来源，不是另一个登记器。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# 列目录本身的超时：沙箱里跑一条 find，正常几十毫秒。
_LIST_TIMEOUT_S = 20
# 一轮最多取回多少个文件，避免一条命令产出上千个文件时把取回变成长尾。剩下的下一轮再取。
_MAX_FETCH_PER_ROUND = 200
_LIST_SEPARATOR = "---jx-myspace---"
# 每个会话上一轮搬运的时刻和当时的路径清单。
#
# 时刻用来给 md5 设下界：不设的话，一个存了上千个文件的账号每跑一条命令就要在沙箱里把整个
# 目录哈希一遍、本地再读一遍，开销与文件总量成正比地白涨。
#
# 路径清单用来认出删除：拿上一轮的清单和这一轮的差集就行，不必再去遍历本机整棵镜像树。
_last_reflect: "OrderedDict[str, tuple[float, frozenset[str]]]" = OrderedDict()
_LAST_REFLECT_MAX = 512


async def reflect_sandbox_myspace(*, session_id: Optional[str], user_id: Optional[str]) -> None:
    """把沙箱 ``/myspace`` 里的现状（新增/改动/删除）搬进本机镜像目录。

    沙箱目录就是镜像目录时直接返回。任何一步失败都只告警：搬不过来最多是这一轮没登记，
    不该影响调用方本身的结果。
    """
    if not user_id:
        return
    from core.sandbox import get_sandbox_provider

    try:
        provider = get_sandbox_provider()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-sandbox-sync] 取 provider 失败: %s", exc)
        return
    if getattr(provider, "myspace_mirror_live", False):
        return  # 沙箱写的就是镜像目录，监听器已经看到了

    key = session_id or user_id
    started = time.time()
    previous = _last_reflect.get(key)
    from core.space_sync.index import reflection

    if previous is None:
        saved = await asyncio.to_thread(reflection, user_id, key)
        if saved:
            previous = (saved[0], frozenset(saved[1]))
    since = previous[0] if previous else None
    try:
        listing = await _list_sandbox_myspace(session_id, user_id, since_ts=since)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[myspace-sandbox-sync] 列沙箱目录失败: %s", exc)
        return
    if listing is None:
        return
    digests, present = listing
    from core.sandbox._common import myspace_cache_dir
    from core.space_sync.files import source_path

    def valid(rel):
        try:
            source_path(myspace_cache_dir(user_id), rel)
            return True
        except (ValueError, OSError):
            return False
        except Exception:
            return False

    present = {rel for rel in present if valid(rel)}
    digests = {rel: digest for rel, digest in digests.items() if rel in present}

    changed = await asyncio.to_thread(_changed_rels, user_id, digests)
    # 第一次见这个会话时没有上一轮清单可比，这一轮不判删除，只把清单记下来。
    removed = sorted(previous[1] - present) if previous else []

    from core.llm.tools import myspace_vfs as _ms
    from core.sandbox import SandboxConnectError, SandboxError
    from core.sandbox._common import WORKSPACE

    base = f"{WORKSPACE}/myspace/{user_id}"
    fetched = 0
    incomplete = len(changed) > _MAX_FETCH_PER_ROUND
    for rel in changed[:_MAX_FETCH_PER_ROUND]:
        try:
            # cube provider 返回 bytearray，而 OSS put_object 会把非 bytes 当文件对象处理，
            # 统一转成 bytes。
            data = bytes(await provider.get_file(session_id, f"{base}/{rel}", user_id=user_id))
        except (SandboxError, SandboxConnectError) as exc:
            incomplete = True
            logger.warning("[myspace-sandbox-sync] 取回 %s 失败: %s", rel, exc)
            continue
        try:
            from core.myspace.projection import write_projection

            await asyncio.to_thread(write_projection, user_id, rel, data, None)
        except Exception as exc:
            incomplete = True
            logger.warning("[myspace-sandbox-sync] 缓存写入 %s 失败: %s", rel, exc)
            continue
        fetched += 1
    failed_removals = set()
    from core.space_sync.files import delete_source

    for rel in removed:
        try:
            await asyncio.to_thread(delete_source, myspace_cache_dir(user_id), rel)
        except FileNotFoundError:
            pass
        except Exception as exc:
            failed_removals.add(rel)
            incomplete = True
            logger.warning("[myspace-sandbox-sync] 缓存删除 %s 失败: %s", rel, type(exc).__name__)
    present |= failed_removals
    # 留一点余量：命令收尾那一刻正在写的文件下一轮还会被看到，不会漏。
    _last_reflect[key] = (since if incomplete else started - 2.0, frozenset(present))
    await asyncio.to_thread(reflection, user_id, key, [_last_reflect[key][0], sorted(present)])
    _last_reflect.move_to_end(key)
    while len(_last_reflect) > _LAST_REFLECT_MAX:
        _last_reflect.popitem(last=False)
    if fetched or removed:
        logger.info(
            "[myspace-sandbox-sync] user=%s 取回 %d 个、移除 %d 个（登记交给监听器）",
            user_id,
            fetched,
            len(removed),
        )


async def _list_sandbox_myspace(
    session_id: Optional[str], user_id: str, *, since_ts: Optional[float] = None
) -> Optional[tuple[dict[str, str], set[str]]]:
    """列出沙箱 myspace 下的 ``({相对路径: md5}, {全部相对路径})``。

    md5 只算两类文件：大小在生成物上限内的（超限的本来也不登记），以及 ``since_ts``
    之后动过的（没动过的内容不可能变）。但**路径清单是全量的**，而且不带任何过滤 ——
    少列一个路径就会被当成"沙箱里删掉了"，把用户的文件从「我的空间」里抹掉。
    """
    from core.config.settings import settings
    from core.llm.tools._common import sandbox_exec_bash, shell_quote
    from core.sandbox._common import WORKSPACE

    base = f"{WORKSPACE}/myspace/{user_id}"
    max_bytes = settings.sandbox.artifact_max_bytes
    newer = (
        ""
        if since_ts is None
        else f"\\( -newermt @{int(since_ts)} -o -newerct @{int(since_ts)} \\) "
    )
    cmd = (
        f"cd {shell_quote(base)} 2>/dev/null || exit 0; "
        f"find . -type f {newer}-size -{max_bytes + 1}c -exec md5sum {{}} + 2>/dev/null; "
        f"echo {_LIST_SEPARATOR}; "
        "find . -type f 2>/dev/null"
    )
    code, out, _err = await sandbox_exec_bash(
        cmd, chat_id=session_id, user_id=user_id, timeout=_LIST_TIMEOUT_S
    )
    if code != 0:
        return None

    digest_text, _, path_text = out.partition(_LIST_SEPARATOR)
    digests: dict[str, str] = {}
    for line in digest_text.strip().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        rel = parts[1].strip().removeprefix("./")
        if rel:
            digests[rel] = parts[0]
    present = {ln.strip().removeprefix("./") for ln in path_text.strip().splitlines() if ln.strip()}
    return digests, present


def _changed_rels(user_id: str, digests: dict[str, str]) -> list[str]:
    """镜像里没有、或者内容和沙箱对不上的相对路径。"""
    from core.llm.tools import myspace_vfs as _ms

    out: list[str] = []
    for rel, sandbox_md5 in digests.items():
        try:
            fp = _ms.myspace_cache_file(user_id, rel)
            from core.space_sync.files import read_source
            from core.sandbox._common import myspace_cache_dir

            cache_md5 = (
                hashlib.md5(read_source(myspace_cache_dir(user_id), rel)).hexdigest()
                if fp.is_file()
                else None
            )
        except Exception:
            cache_md5 = None
        if cache_md5 != sandbox_md5:
            out.append(rel)
    return sorted(out)
