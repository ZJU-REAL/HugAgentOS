"""共享产物库读取 + 技能/插件包安全解包（skill-manager 与 plugin-manager 共用）。

这些动作原本只长在 ``skill_manager_mcp/impl.py`` 里；plugin-manager 的 ``import_plugin``
走的是同一条通路（沙箱产出目录 → tar/zip → sandbox_get_artifact → 本进程解包落库），
所以抽到这里共用，避免第二份拷贝各自漂移——尤其是 zip-bomb 与目录穿越这两道防护，
复制一次就少一次被同步修补的机会。

MCP 容器与 backend 挂同一 ``/app/storage`` 卷，故读得到共享产物库（但读不到沙箱本身）。
"""

from __future__ import annotations

import io
import logging
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# tar/zip 解包安全上限（防 zip-bomb / 超大产物）
MAX_ENTRIES = 4000
MAX_TOTAL_BYTES = 64 * 1024 * 1024  # 64 MB 解压后总量


def read_artifact_bytes(file_id: str) -> Optional[bytes]:
    """从共享产物库按 file_id 读回字节（local: 读 path；oss: download_bytes）。"""
    try:
        from core.artifacts import store

        meta = store.get_artifact(file_id)
        if not meta:
            return None
        path = meta.get("path")
        if path and os.path.isfile(path):
            with open(path, "rb") as fh:
                return fh.read()
        key = meta.get("storage_key")
        if key:
            from core.storage import get_storage

            return bytes(get_storage().download_bytes(key))
    except Exception as exc:  # noqa: BLE001
        logger.warning("read artifact %s failed (%s)", file_id, exc)
    return None


def looks_like_zip(data: bytes) -> bool:
    return data[:2] == b"PK"


def within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def safe_extract(data: bytes, dest: Path) -> None:
    """安全解包 tar(.gz) 或 zip 到 dest：拦目录穿越 + 限条目数/总量。"""
    if looks_like_zip(data):
        safe_extract_zip(data, dest)
    else:
        safe_extract_tar(data, dest)


def safe_extract_tar(data: bytes, dest: Path) -> None:
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        members = tf.getmembers()
        if len(members) > MAX_ENTRIES:
            raise ValueError(f"包内条目过多（{len(members)} > {MAX_ENTRIES}）")
        for m in members:
            if not (m.isfile() or m.isdir()):
                continue  # 跳过软链接/设备等，杜绝越权
            target = dest / m.name
            if not within(dest, target):
                raise ValueError(f"检测到目录穿越条目：{m.name}")
            total += max(m.size, 0)
            if total > MAX_TOTAL_BYTES:
                raise ValueError("解压后总量超限（>64MB）")
        tf.extractall(dest, members=[m for m in members if m.isfile() or m.isdir()])


def safe_extract_zip(data: bytes, dest: Path) -> None:
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise ValueError(f"包内条目过多（{len(infos)} > {MAX_ENTRIES}）")
        for info in infos:
            target = dest / info.filename
            if not within(dest, target):
                raise ValueError(f"检测到目录穿越条目：{info.filename}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("解压后总量超限（>64MB）")
        zf.extractall(dest)


def locate_root(extracted: Path) -> Optional[Path]:
    """定位技能/插件根：含 SKILL.md 或插件清单的目录。tar 常多包一层。"""

    def _is_root(d: Path) -> bool:
        return (d / "SKILL.md").is_file() or is_plugin_root(d)

    if _is_root(extracted):
        return extracted
    subdirs = [p for p in extracted.iterdir() if p.is_dir()]
    for d in subdirs:
        if _is_root(d):
            return d
    # 再下探一层
    for d in subdirs:
        for dd in (p for p in d.iterdir() if p.is_dir()):
            if _is_root(dd):
                return dd
    return None


def is_plugin_root(root: Path) -> bool:
    """该目录是否是插件包根。

    判定必须与导入器一致：``plugin_importer.detect_manifest`` 认原生 / .claude-plugin /
    .codex-plugin 三种布局，这里若自己复述一遍规则，codex 布局的包就会被本地拦下、
    却能从后台上传导入——同一个包两条通路结论不同。所以直接问导入器。
    """
    try:
        from core.services.plugin_importer import detect_manifest

        detect_manifest(root)
        return True
    except Exception:  # noqa: BLE001 — 非插件包（含 BadRequestError）一律按 False
        return False
