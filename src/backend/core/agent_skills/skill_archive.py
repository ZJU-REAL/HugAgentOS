"""Content-addressed archives for sandbox skill delivery.

Archive identity includes the complete file set, bytes and executable modes.
A cache in another backend process cannot deliver an earlier skill revision.
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

from .publication import publication_lock, read_tree, safe_skill_id


def _archives_dir() -> Path:
    from .config import get_sandbox_skills_dir

    path = get_sandbox_skills_dir().parent / "skill_archives"
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_skill_tar(skill_id: str, src: Path) -> Optional[Path]:
    safe_skill_id(skill_id)
    with publication_lock():
        if not src.is_dir():
            return None
        files = read_tree(src)
        modes = {name: (src / name).stat().st_mode & 0o777 for name in files}
        digest = hashlib.sha256()
        for name, data in sorted(files.items()):
            encoded = name.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(modes[name].to_bytes(4, "big"))
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        path = _archives_dir() / f"{skill_id}-{digest.hexdigest()}.tgz"
        if path.is_file():
            return path
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temp_path = Path(temporary.name)
        try:
            with tarfile.open(temp_path, "w:gz", compresslevel=1) as archive:
                for name, data in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mode = modes[name]
                    archive.addfile(info, io.BytesIO(data))
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
        return path


def build_current_skill_tar(skill_id: str, user_id: str | None = None) -> Optional[Path]:
    """Resolve visibility and freeze current bytes under one publication lock."""
    from .publication import prepare_skill_view
    from .config import get_sandbox_skills_dir

    safe_skill_id(skill_id)
    with publication_lock():
        view = prepare_skill_view(user_id)
        root = view if view is not None else get_sandbox_skills_dir()
        return build_skill_tar(skill_id, root / skill_id)
