"""Read-only inventory of unregistered skill folders. Installation is explicit."""
import re
from . import archive
from .errors import CapabilityError
from .paths import capabilities_enabled, kind_root, assert_managed_path


def scan(user_id):
    if not user_id or not capabilities_enabled():
        return []
    pending = []
    root = kind_root("skill")
    for parent in (root, root / "local"):
        if not parent.is_dir():
            continue
        try:
            assert_managed_path(parent)
            folders = sorted(parent.iterdir())
        except (CapabilityError, OSError):
            continue
        for folder in folders:
            try:
                if parent == root and (folder.name in ("local", "builtin")
                        or re.fullmatch(r"p_[0-9a-f]{10,32}", folder.name)):
                    continue
                if not folder.is_dir() or not (folder / "SKILL.md").is_file():
                    continue
                assert_managed_path(folder)
                list(archive.iter_files(folder))
                pending.append({"folder": folder.name, "path": str(folder), "code": "pending_import"})
            except (CapabilityError, OSError, ValueError) as exc:
                pending.append({"folder": folder.name, "code": getattr(exc, "code", "invalid_skill")})
    return pending
