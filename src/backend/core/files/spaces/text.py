"""UTF-8 editing for authorized mounted files."""
import hashlib

from fastapi import HTTPException
from core.files.artifacts.text import decode_text, MAX_TEXT_BYTES, TEXT_EXTENSIONS
from core.files.spaces.leases import authorized_operation


class SpaceText:
    def __init__(self, files):
        self.files = files
        self.db, self.actor = files.db, files.actor

    def _load(self, space, path, *, write=False):
        grant = self.files.grant(space, write=write)
        key = grant.key(path)
        info = self.files.storage.stat_object(key)
        if info is None or info.is_directory:
            raise HTTPException(404, "File not found")
        if info.name.rsplit(".", 1)[-1].lower() not in TEXT_EXTENSIONS:
            raise HTTPException(415, "此格式不支持在线编辑")
        if info.size > MAX_TEXT_BYTES:
            raise HTTPException(413, "在线编辑最多支持 2 MiB 文本")
        data = self.files.storage.download_bytes(key)
        return grant, info, data

    @staticmethod
    def document(grant, info, data):
        return {
            "name": info.name, "content": decode_text(data),
            "revision": hashlib.sha256(data).hexdigest(),
            "size": len(data), "can_edit": grant.writable,
        }

    @authorized_operation
    def read(self, space, path):
        return self.document(*self._load(space, path))

    @authorized_operation
    def save(self, space, path, content, revision):
        grant, info, previous = self._load(space, path, write=True)
        if hashlib.sha256(previous).hexdigest() != revision:
            raise HTTPException(409, "文件已被修改，请保留草稿并重新打开文件后合并")
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HTTPException(415, "在线编辑仅支持 UTF-8 文本") from exc
        decode_text(data)
        self.files._write(space, path, data)
        return self.document(grant, info, data)
