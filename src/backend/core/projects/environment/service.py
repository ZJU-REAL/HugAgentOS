"""Project capability configuration; permission checks remain user scoped."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

from core.projects.environment.config import FIELDS, ProjectEnvironment
from fastapi import HTTPException


class ProjectEnvironmentService:
    def __init__(self, db):
        self.db = db

    def read(self, project):
        return ProjectEnvironment.model_validate(
            (project.extra_data or {}).get("environment") or {}
        ).model_dump()

    @staticmethod
    def _validate_bindings(config, graph):
        if config["mode"] == "custom":
            for field in FIELDS:
                if set(config[field]) - set(graph[field]):
                    raise HTTPException(422, "所选资源不可用或未获授权，请刷新资源列表")
        return config

    def validate(self, user_id, data):
        config = ProjectEnvironment.model_validate(data).model_dump()
        if config["mode"] == "custom" and any(config[field] for field in FIELDS):
            from core.projects.environment_resources import environment_resources

            self._validate_bindings(config, environment_resources(self.db, user_id))
        return config

    def preview(self, user_id, data):
        from core.projects.environment.dependencies import expand_environment
        from core.projects.environment_resources import environment_resources

        config = ProjectEnvironment.model_validate(data).model_dump()
        graph = environment_resources(self.db, user_id)
        self._validate_bindings(config, graph)
        return expand_environment(config, graph)

    @contextmanager
    def creation_metadata(self, user_id, config):
        """Join the project's existing creation transaction, including folder creation."""
        from core.db.models import Project
        from sqlalchemy import event

        def attach_metadata(session, _flush_context, _instances):
            for project in session.new:
                if isinstance(project, Project) and project.owner_user_id == user_id:
                    project.extra_data = {
                        **(project.extra_data or {}),
                        "environment": config,
                        "environment_revision": uuid4().hex,
                    }

        event.listen(self.db, "before_flush", attach_metadata)
        try:
            yield
        finally:
            event.remove(self.db, "before_flush", attach_metadata)

    def save(self, project, user_id, data):
        config = self.validate(user_id, data)
        # Lock the row before merging JSON so concurrent memory/environment
        # changes cannot overwrite one another's metadata.
        self.db.refresh(project, with_for_update=True)
        project.extra_data = {
            **(project.extra_data or {}),
            "environment": config,
            "environment_revision": uuid4().hex,
        }
        self.db.commit()
        return self.read(project)

    def describe(self, project, user_id):
        from core.projects.environment.dependencies import expand_environment
        from core.projects.environment_resources import environment_resources

        config = self.read(project)
        graph = environment_resources(self.db, user_id)
        resolved = expand_environment(config, graph)
        return {
            "config": config,
            "revision": (project.extra_data or {}).get("environment_revision", ""),
            **resolved,
        }
