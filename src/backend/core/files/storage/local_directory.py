"""Directory operations for a local storage root."""
import os
import stat
from datetime import datetime, timezone
from core.files.storage.entries import DirectoryPage, StorageEntry, page_limit, validate_key
from core.files.storage.local_paths import directory_fd, file_stat, parent_fd


def entry(name, key, info):
    is_dir = stat.S_ISDIR(info.st_mode)
    return StorageEntry(name, key + ("/" if is_dir else ""), is_dir,
        0 if is_dir else info.st_size, datetime.fromtimestamp(info.st_mtime, timezone.utc),
        f"{info.st_ino:x}-{info.st_mtime_ns:x}-{info.st_size:x}")


class LocalDirectoryOperations:
    def list_directory(self, prefix: str, *, cursor=None, limit=200) -> DirectoryPage:
        prefix = validate_key(prefix, directory=True)
        page_limit(limit)
        self._get_full_path(prefix, directory=True)
        entries = []
        try:
            if os.name == "posix":
                with directory_fd(self.base_path, prefix) as descriptor:
                    for name in sorted(os.listdir(descriptor)):
                        if cursor is not None and name <= cursor:
                            continue
                        try:
                            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                            entries.append(entry(name, prefix + name, info))
                        if len(entries) > limit:
                            break
            else:
                root = self._get_full_path(prefix, directory=True)
                for path in sorted(root.iterdir()):
                    if path.is_symlink() or cursor is not None and path.name <= cursor:
                        continue
                    entries.append(entry(path.name, prefix + path.name, path.stat()))
                    if len(entries) > limit:
                        break
        except (FileNotFoundError, NotADirectoryError):
            return DirectoryPage(())
        return DirectoryPage(tuple(entries[:limit]), entries[limit - 1].name if len(entries) > limit else None)

    def stat_object(self, key):
        key = validate_key(key, directory=key.endswith("/")).rstrip("/")
        try:
            info = file_stat(self.base_path, key) if os.name == "posix" else self._get_full_path(key).stat()
        except FileNotFoundError:
            return None
        return entry(key.rsplit("/", 1)[-1], key, info)

    def make_directory(self, prefix):
        if os.name == "posix":
            with directory_fd(self.base_path, prefix, create=True):
                return
        self._get_full_path(prefix, directory=True).mkdir(parents=True, exist_ok=True)

    def copy_object(self, source, destination):
        self.upload_bytes(self.download_bytes(source), destination)

    def remove_directory(self, prefix):
        key = validate_key(prefix, directory=True).rstrip("/")
        if os.name == "posix":
            with parent_fd(self.base_path, key) as (descriptor, name):
                os.rmdir(name, dir_fd=descriptor)
        else:
            self._get_full_path(key).rmdir()
