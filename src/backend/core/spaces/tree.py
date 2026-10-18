"""Bounded traversal and directory-safe storage writes."""
from fastapi import HTTPException


def ensure_parents(storage, key):
    parts = key.rstrip("/").split("/")
    for index in range(1, len(parts)):
        entry = storage.stat_object("/".join(parts[:index]))
        if entry is not None and not entry.is_directory:
            raise HTTPException(409, "A parent path is a file")


def tree_entries(storage, prefix, *, limit=10000):
    pending, result, visited = [prefix], [], set()
    while pending:
        directory = pending.pop()
        if directory in visited:
            raise HTTPException(502, "Storage returned a repeated directory")
        visited.add(directory)
        cursor, cursors = None, set()
        while True:
            page = storage.list_directory(directory, cursor=cursor)
            for item in page.entries:
                suffix = item.key[len(directory):].rstrip("/")
                if not item.key.startswith(directory) or not suffix or "/" in suffix:
                    raise HTTPException(502, "Storage returned an invalid path")
                result.append(item)
                if len(result) > limit:
                    raise HTTPException(413, "Directory exceeds operation limit")
                if item.is_directory:
                    pending.append(item.key)
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in cursors:
                raise HTTPException(502, "Storage returned a repeated cursor")
            cursors.add(cursor)
    return result


def copy_tree(storage, source, destination, entries):
    storage.make_directory(destination)
    for item in entries:
        target = destination + item.key[len(source):]
        if item.is_directory:
            storage.make_directory(target)
        else:
            storage.copy_object(item.key, target)


def remove_tree(storage, prefix, entries):
    for item in sorted(entries, key=lambda entry: len(entry.key), reverse=True):
        if item.is_directory:
            storage.remove_directory(item.key)
        else:
            storage.delete(item.key)
    storage.remove_directory(prefix)
