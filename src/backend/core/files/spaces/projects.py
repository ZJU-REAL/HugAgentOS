"""Resolve project bindings into object-storage paths."""
from urllib.parse import urlencode
import mimetypes
from fastapi import HTTPException
from core.db.models import UserFolder
from core.files.spaces.access import namespace
from core.files.storage.entries import validate_key


def folder_path(db, model, owner_filter, folder_id):
    parts, visited = [], set()
    while folder_id:
        if folder_id in visited:
            raise HTTPException(409, "文件夹层级无效")
        visited.add(folder_id)
        row = db.query(model).filter_by(**owner_filter, folder_id=folder_id, deleted_at=None).first()
        if row is None:
            raise HTTPException(409, "项目文件夹不存在")
        name = validate_key(row.name)
        if "/" in name:
            raise HTTPException(409, "文件夹名称无效")
        parts.append(name)
        folder_id = row.parent_folder_id
    return "/".join(reversed(parts))


def project_location(db, project):
    if project.kind == "personal" and project.linked_folder_id:
        path = folder_path(db, UserFolder, {"user_id": project.owner_user_id}, project.linked_folder_id)
        return "personal", path, namespace("users", project.owner_user_id) + path + "/"
    from core.files.edition import project_location as edition_location
    location = edition_location(db, project)
    if location is not None:
        return location
    raise HTTPException(409, "项目未绑定文件目录")


def source_key(db, project, relative):
    try:
        relative = validate_key(relative)
    except ValueError as exc:
        raise HTTPException(400, "源码路径必须是项目内相对路径") from exc
    return project_location(db, project)[2] + relative


def project_entries(db, project, storage):
    space, path, root = project_location(db, project)
    pending, entries = [root], []
    while pending:
        prefix = pending.pop()
        cursor = None
        while True:
            page = storage.list_directory(prefix, cursor=cursor)
            for item in page.entries:
                if not item.key.startswith(root):
                    raise HTTPException(502, "存储服务返回了无效路径")
                relative = item.key[len(root):]
                validate_key(relative, directory=item.is_directory)
                if item.is_directory:
                    pending.append(item.key)
                else:
                    entries.append({
                        "id": relative, "artifact_id": relative, "name": relative,
                        "title": item.name, "size_bytes": item.size,
                        "mime_type": mimetypes.guess_type(item.name)[0] or "application/octet-stream",
                        "type": "other",
                        "folder_path": relative.rpartition("/")[0],
                        "download_url": "/v1/myspace/content?" + urlencode({"space": space, "path": path + "/" + relative}),
                        "storage_key": item.key, "created_at": item.modified_at,
                    })
                if len(entries) + len(pending) > 10000:
                    raise HTTPException(413, "项目文件数量超过限制")
            cursor = page.next_cursor
            if not cursor:
                break
    return sorted(entries, key=lambda item: item["name"])
