"""Extract and locate plugin archives without accessing installed state."""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

from core.infra.exceptions import BadRequestError

from . import definitions as plugin_definitions


def _locate_plugin_root(extract_dir: Path) -> Path:
    """Locate the plugin root in the extraction dir: prefer the directory containing a manifest; zips often wrap an extra directory level."""

    def _has_manifest(d: Path) -> bool:
        return (
            (d / "plugin.json").is_file()
            or (d / ".claude-plugin" / "plugin.json").is_file()
            or (d / ".codex-plugin" / "plugin.json").is_file()
        )

    if _has_manifest(extract_dir):
        return extract_dir
    subdirs = [c for c in extract_dir.iterdir() if c.is_dir()]
    if len(subdirs) == 1 and _has_manifest(subdirs[0]):
        return subdirs[0]
    for d in sorted(extract_dir.rglob("*")):
        if d.is_dir() and _has_manifest(d):
            return d
    raise BadRequestError(
        message="zip 内未找到插件清单（plugin.json / .claude-plugin / .codex-plugin）"
    )


@contextmanager
def _extract_plugin_zip(raw: bytes) -> Iterator[Path]:
    """Extract the plugin zip to a temp dir and locate the plugin root; yields that root dir and cleans up the temp dir on exit."""
    if len(raw) > plugin_definitions.MAX_ZIP_BYTES:
        raise BadRequestError(
            message=f"插件包过大（>{plugin_definitions.MAX_ZIP_BYTES // (1024 * 1024)}MB）"
        )
    tmp_root = Path(tempfile.mkdtemp(prefix="plugin_import_"))
    try:
        extract_dir = tmp_root / "extracted"
        extract_dir.mkdir()
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                seen_targets: set[str] = set()
                for member in zf.infolist():
                    # Some Windows ZIP writers store backslashes literally.
                    # Normalize them before both traversal checks and extraction
                    # so skills\foo\SKILL.md remains a portable skill directory.
                    normalized_name = member.filename.replace("\\", "/")
                    member_path = PurePosixPath(normalized_name)
                    parts = member_path.parts
                    if (
                        not normalized_name
                        or not parts
                        or member_path.is_absolute()
                        or ".." in parts
                        or (parts and parts[0].endswith(":"))
                    ):
                        raise BadRequestError(message=f"非法压缩包条目：{member.filename}")
                    target_key = "/".join(parts)
                    if target_key in seen_targets:
                        raise BadRequestError(message=f"压缩包包含重复条目：{normalized_name}")
                    seen_targets.add(target_key)
                    target = extract_dir.joinpath(*parts)
                    if member.is_dir() or normalized_name.endswith("/"):
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as source_file, target.open("wb") as target_file:
                        shutil.copyfileobj(source_file, target_file)
        except zipfile.BadZipFile:
            raise BadRequestError(message="不是有效的 zip 文件")
        yield _locate_plugin_root(extract_dir)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
