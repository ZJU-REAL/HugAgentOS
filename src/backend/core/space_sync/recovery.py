"""One startup metadata scan restores identity by inode, never by coincident content."""

import os
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from .content import signature
from .index import snapshot


def recover_personal(root, excluded, emit):
    for user_root in root.iterdir():
        if not user_root.is_dir() or user_root.is_symlink():
            continue
        owner = user_root.name
        tracked = snapshot(owner)
        present = {}
        for parent, dirs, files in os.walk(user_root, followlinks=False):
            dirs[:] = [d for d in dirs if d not in excluded and not Path(parent, d).is_symlink()]
            for name in dirs + files:
                path = Path(parent, name)
                if path.is_symlink():
                    continue
                present[path.relative_to(user_root).as_posix()] = signature(path)
        moved = []
        for old, state in sorted(
            tracked.items(), key=lambda item: (not item[1].get("directory"), item[0].count("/"))
        ):
            if old in present or any(old.startswith(source + "/") for source, dest in moved):
                continue
            candidates = [
                p
                for p, s in present.items()
                if p not in tracked and s[:2] == (state.get("signature") or [])[:2]
            ]
            if len(candidates) == 1:
                new = candidates[0]
                cls = DirMovedEvent if state.get("directory") else FileMovedEvent
                emit(cls(str(user_root / old), str(user_root / new)))
                moved.append((old, new))
            else:
                cls = DirDeletedEvent if state.get("directory") else FileDeletedEvent
                emit(cls(str(user_root / old)))
        for relative, stat in present.items():
            if any(relative == dest for source, dest in moved):
                continue
            if any(relative.startswith(dest + "/") for source, dest in moved):
                if (user_root / relative).is_file():
                    emit(FileModifiedEvent(str(user_root / relative)))
                continue
            path = user_root / relative
            emit(DirCreatedEvent(str(path)) if path.is_dir() else FileModifiedEvent(str(path)))
