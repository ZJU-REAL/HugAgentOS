"""Chat context reads instructions without enumerating project directories."""
import pytest
from core.services.project_scope import build_project_ctx
from core.services.project_service import ProjectService
from tests.services.project_instructions_support import env, local, update


@pytest.mark.parametrize("kind", ["personal", "local"])
def test_project_context_does_not_enumerate_files(env, monkeypatch, kind):
    db, _ = env
    if kind == "local":
        project, instruction_path = local(env)
        instruction_path.write_text("Keep project boundaries.\n")
    else:
        project = ProjectService(db).create_personal("alice", "Context")
        update(db, project, "Keep project boundaries.\n")

    calls = []

    def forbidden_listing(*args, **kwargs):
        calls.append("directory enumeration")
        raise AssertionError("Sending a project message must not enumerate files")

    monkeypatch.setattr("core.services.project_file_service.ProjectFileService.list_files", forbidden_listing)
    monkeypatch.setattr("core.sandbox.local_mount.list_local_files", forbidden_listing)
    monkeypatch.setattr("core.sandbox.local_mount.ensure_local_project_link", lambda *args: None)
    build = build_project_ctx
    # This generated checkout already uses the CE project-scope implementation.

    for _ in range(2):
        context = build(db, project.project_id)
        assert context["project_instructions"] == "Keep project boundaries.\n"
        assert context["project_id"] == project.project_id
        assert "project_files" not in context
        assert "project_file_count" not in context
    assert calls == []
