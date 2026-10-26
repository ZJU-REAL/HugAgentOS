"""File operations within an authenticated space grant."""
from fastapi import HTTPException
from core.files.spaces.access import require_grant
from core.files.spaces.leases import authorized_operation
from core.files.spaces.search import search_directory
from core.files.spaces.tree import ensure_parents, tree_entries, copy_tree, remove_tree

MAX_TRANSFER_BYTES = 50 * 1024 * 1024

class SpaceFiles:
    def __init__(self, db, actor, storage):
        self.db, self.actor, self.storage = db, actor, storage

    def grant(self, space, *, write=False):
        return require_grant(self.db, self.actor, space, write=write)

    @authorized_operation
    def list(self, space, path="", *, cursor=None, limit=200, keyword=""):
        grant = self.grant(space)
        prefix = grant.key(path, directory=True)
        needle = keyword.strip().lower()
        page = (search_directory(self.storage, prefix, needle, cursor=cursor, limit=limit)
                if needle else self.storage.list_directory(prefix, cursor=cursor, limit=limit))
        entries = []
        for item in page.entries:
            if not item.key.startswith(grant.prefix):
                raise HTTPException(502, "Storage returned an invalid path")
            relative = item.key[len(grant.prefix):].rstrip("/")
            grant.key(relative)
            entries.append({
                "name": item.name, "path": relative, "is_directory": item.is_directory,
                "size": item.size, "modified_at": item.modified_at,
                "etag": item.etag, "version": item.version,
            })
        return {"space": grant.public(), "path": path, "entries": entries,
            "next_cursor": page.next_cursor}

    @authorized_operation
    def read(self, space, path):
        grant = self.grant(space)
        key = grant.key(path)
        info = self.storage.stat_object(key)
        if info is None or info.is_directory:
            raise HTTPException(404, "File not found")
        if info.size > MAX_TRANSFER_BYTES:
            raise HTTPException(413, "File exceeds transfer limit")
        return self.storage.download_bytes(key), info

    def occupied(self, space, path):
        """A file, directory marker or implicit directory already sits at ``path``."""
        key = self.grant(space).key(path)
        return (self.storage.stat_object(key) is not None or self.storage.stat_object(key + "/") is not None
                or bool(self.storage.list_directory(key + "/", limit=1).entries))

    @authorized_operation
    def write(self, space, path, data):
        return self._write(space, path, data)

    def _write(self, space, path, data):
        key = self._file_target(space, path, len(data))
        self.storage.upload_bytes(data, key)
        return {"path": path}

    @authorized_operation
    def copy_from(self, space, path, source_key, size):
        """Server-side copy of an object stored elsewhere (an upload snapshot); bytes never pass through us."""
        key = self._file_target(space, path, size)
        self.storage.copy_object(source_key, key)
        return {"path": path}

    def _file_target(self, space, path, size):
        grant = self.grant(space, write=True)
        key = grant.key(path)
        if size > MAX_TRANSFER_BYTES:
            raise HTTPException(413, "File exceeds transfer limit")
        ensure_parents(self.storage, key)
        existing = self.storage.stat_object(key)
        marker = self.storage.stat_object(key + "/")
        if (existing and existing.is_directory) or (marker and marker.is_directory) or self.storage.list_directory(key + "/", limit=2).entries:
            raise HTTPException(409, "A directory exists at that path")
        return key

    @authorized_operation
    def create(self, space, path, data, *, idempotent=False):
        grant = self.grant(space, write=True)
        key = grant.key(path)
        existing = self.storage.stat_object(key)
        if existing is not None:
            if idempotent and not existing.is_directory and existing.size == len(data) and self.storage.download_bytes(key) == data:
                return {"path": path}
            raise HTTPException(409, "目标位置已有同名文件，请更换文件名")
        return self._write(space, path, data)

    @authorized_operation
    def mkdir(self, space, path):
        grant = self.grant(space, write=True)
        key = grant.key(path)
        if self.storage.stat_object(key) is not None:
            raise HTTPException(409, "A file exists at that path")
        ensure_parents(self.storage, key)
        self.storage.make_directory(key + "/")
        return {"path": path}

    @authorized_operation
    def delete(self, space, path, *, directory=False):
        grant = self.grant(space, write=True)
        key = grant.key(path)
        if directory:
            try:
                self.storage.remove_directory(key + "/")
            except OSError as exc:
                raise HTTPException(409, "Directory is not empty") from exc
        else:
            self.storage.delete(key)

    @authorized_operation
    def move(self, space, source, destination):
        grant = self.grant(space, write=True)
        src, dst = grant.key(source), grant.key(destination)
        if src == dst:
            return {"path": destination}
        info = self.storage.stat_object(src)
        marker = self.storage.stat_object(src + "/")
        if info and info.is_directory or marker and marker.is_directory or self.storage.list_directory(src + "/", limit=2).entries:
            return self._move_directory(grant, source, destination)
        if info is None:
            raise HTTPException(404, "Source file not found")
        if self.storage.stat_object(dst) is not None or self.storage.list_directory(dst + "/", limit=1).entries:
            raise HTTPException(409, "Destination exists")
        ensure_parents(self.storage, dst)
        self.storage.copy_object(src, dst)
        self.storage.delete(src)
        return {"path": destination}


    def _move_directory(self, grant, source, destination):
        src, dst = grant.key(source), grant.key(destination)
        if dst.startswith(src + "/"):
            raise HTTPException(400, "A directory cannot be moved into itself")
        if self.storage.stat_object(dst) or self.storage.stat_object(dst + "/") or self.storage.list_directory(dst + "/", limit=1).entries:
            raise HTTPException(409, "Destination exists")
        ensure_parents(self.storage, dst)
        objects = tree_entries(self.storage, src + "/")
        copy_tree(self.storage, src + "/", dst + "/", objects)
        remove_tree(self.storage, src + "/", objects)
        return {"path": destination}

    @authorized_operation
    def copy(self, space, source, target_space, destination):
        origin = self.grant(space)
        target = self.grant(target_space, write=True)
        src, dst = origin.key(source), target.key(destination)
        if dst == src or dst.startswith(src + "/"):
            raise HTTPException(400, "Invalid copy destination")
        ensure_parents(self.storage, dst)
        if self.storage.stat_object(dst) or self.storage.stat_object(dst + "/") or self.storage.list_directory(dst + "/", limit=1).entries:
            raise HTTPException(409, "Destination exists")
        info = self.storage.stat_object(src)
        children = self.storage.list_directory(src + "/", limit=1).entries
        if (info and info.is_directory) or self.storage.stat_object(src + "/") or children:
            entries = tree_entries(self.storage, src + "/")
            copy_tree(self.storage, src + "/", dst + "/", entries)
            return {"path": destination, "files": sum(not item.is_directory for item in entries)}
        if info is None:
            raise HTTPException(404, "Source file not found")
        self.storage.copy_object(src, dst)
        return {"path": destination, "files": 1}

    @authorized_operation
    def delete_tree(self, space, path):
        grant = self.grant(space, write=True)
        prefix = grant.key(path) + "/"
        entries = tree_entries(self.storage, prefix)
        remove_tree(self.storage, prefix, entries)
        return sum(not item.is_directory for item in entries)
