"""Project committed directory renames only over clean tracked subtrees."""

import hashlib
import os
from pathlib import Path

from .content import artifact_timestamp, signature
from .files import parent_fd, read_source
from .index import move, remember, snapshot


def project_directories(owner, root, rows, resolve, tracked):
    live = {row.folder_id: (resolve(row.folder_id), row) for row in rows}
    blocked = set()
    for old, state in sorted(tracked.items(), key=lambda item: item[0].count("/")):
        if not state.get("directory") or state.get("id") not in live:
            continue
        new, row = live[state["id"]]
        if not new or new == old or not (root / old).is_dir():
            continue
        clean = not (root / new).exists()
        for parent, dirs, files in os.walk(root / old, followlinks=False):
            for name in dirs + files:
                path = Path(parent, name)
                rel = path.relative_to(root).as_posix()
                baseline = tracked.get(rel)
                if path.is_symlink() or not baseline:
                    clean = False
                elif path.is_file() and baseline.get("signature") != signature(path):
                    clean = clean and hashlib.sha256(
                        read_source(root, rel)
                    ).hexdigest() == baseline.get("sha256")
        if not clean:
            blocked.add(new)
            continue
        with parent_fd(root, old) as (source, leaf):
            with parent_fd(root, new, create=True) as (destination, target):
                os.rename(leaf, target, src_dir_fd=source, dst_dir_fd=destination)
        move(owner, old, new, True)
        for rel, baseline in snapshot(owner).items():
            if rel == new or rel.startswith(new + "/"):
                baseline["signature"] = signature(root / rel)
                if baseline.get("directory") and baseline.get("id") in live:
                    baseline["version"] = artifact_timestamp(live[baseline["id"]][1])
                remember(owner, rel, baseline)
    return blocked
