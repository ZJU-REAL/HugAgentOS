"""Files in a project's mounted directory."""
import os
from typing import NamedTuple

from core.files.spaces.projects import project_entries
from core.files.storage import get_storage


class ProjectFileListing(NamedTuple):
    items: list[dict]
    total: int


class ProjectFileService:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def matches_scope(project, scope):
        from core.files.edition import matches_scope
        return matches_scope(project, scope)

    def list_files(self, project, *, limit=None):
        items = project_entries(self.db, project, get_storage())
        return ProjectFileListing(items if limit is None else items[:limit], len(items))

    def capacity_used(self, project):
        return sum(item["size_bytes"] for item in self.list_files(project).items)

    @staticmethod
    def capacity_limit():
        try:
            return max(1, int(os.getenv("PROJECT_FILE_CAPACITY_BYTES", str(200 * 1024 * 1024))))
        except ValueError:
            return 200 * 1024 * 1024

    def upload(self, project, user_id, file_bytes, filename, mime_type=None):
        from core.projects.source import ProjectSourceService
        ProjectSourceService(self.db).write_files(
            project.project_id, user_id, [(filename, file_bytes)],
        )
        return next(item for item in self.list_files(project).items if item["name"] == filename)

    def delete(self, project_id, actor, path):
        from core.projects.source import ProjectSourceService
        from core.files.spaces.files import SpaceFiles
        from core.files.spaces.projects import project_location
        project = ProjectSourceService(self.db).authorized_project(project_id, actor, write=True)
        space, root, _ = project_location(self.db, project)
        SpaceFiles(self.db, actor, get_storage()).delete(space, root + "/" + path)
