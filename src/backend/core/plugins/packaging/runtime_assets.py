"""Retain package assets by content so uploads survive extraction and restart."""
import hashlib
import io
import os
import tempfile
import zipfile
from pathlib import Path
from core.db.models.plugin_resource import PluginResourcePackage

MAX_PACKAGE = 50 * 1024 * 1024

def pack(root):
    content = io.BytesIO()
    total = 0
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(Path(root).rglob("*")):
            if path.is_symlink():
                raise ValueError("package_symlink_not_allowed")
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            total += path.stat().st_size
            if total > MAX_PACKAGE:
                raise ValueError("package_assets_too_large")
            info = zipfile.ZipInfo(path.relative_to(root).as_posix())
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    payload = content.getvalue()
    revision = hashlib.sha256(payload).hexdigest()
    return revision, payload

def retain(db, root, prepared=None):
    revision, payload = prepared or pack(root)
    if db.get(PluginResourcePackage, revision) is None:
        db.add(PluginResourcePackage(revision=revision, archive=payload))
        db.flush()
    return revision

def materialize(db, revision):
    row = db.get(PluginResourcePackage, revision)
    if row is None or hashlib.sha256(row.archive).hexdigest() != revision:
        raise ValueError("package_assets_unavailable")
    cache = Path(os.getenv("PLUGIN_ASSET_CACHE_DIR") or
                 str(Path(tempfile.gettempdir()) / "hugagent-plugin-assets"))
    target = cache / revision
    if target.is_dir():
        return target
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache) as stage:
        root = Path(stage)
        with zipfile.ZipFile(io.BytesIO(row.archive)) as archive:
            for item in archive.infolist():
                path = root / item.filename
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("invalid_package_path")
                archive.extract(item, root)
        try:
            root.rename(target)
        except FileExistsError:
            pass
    return target


def forget_cache(revision):
    """Delete only an exact managed SHA-256 cache child after archive collection."""
    import re
    import shutil
    if not re.fullmatch(r"[a-f0-9]{64}", revision):
        return
    cache = Path(os.getenv("PLUGIN_ASSET_CACHE_DIR") or
                 str(Path(tempfile.gettempdir()) / "hugagent-plugin-assets")).resolve()
    target = cache / revision
    if target.is_symlink() or target.resolve().parent != cache:
        raise ValueError("invalid_package_cache_path")
    if target.is_dir():
        shutil.rmtree(target)
