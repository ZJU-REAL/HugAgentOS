"""Chat context reads instructions without enumerating project directories."""
import importlib.util
import sys
from pathlib import Path

import pytest
from core.projects.edition import build_project_ctx
from core.projects.edition import ProjectService
from tests.services.project_instructions_support import env, local, update


@pytest.mark.parametrize("edition", ["ee", "ce"])
@pytest.mark.parametrize("kind", ["personal", "local"])
def test_project_context_does_not_enumerate_files(env, monkeypatch, edition, kind):
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

    monkeypatch.setattr('core.projects.files.ProjectFileService.list_files', forbidden_listing)
    monkeypatch.setattr("core.sandbox.workspace.mounts.list_local_files", forbidden_listing)
    monkeypatch.setattr("core.sandbox.workspace.mounts.ensure_local_project_link", lambda *args: None)
    build = build_project_ctx
    if edition == "ce":
        root = Path(__file__).resolve().parents[4]
        spec = importlib.util.spec_from_file_location(
            "ce_project_scope_no_inventory", root / "src/backend/core/projects/personal_scope.py"
        )
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
        build = module.build_project_ctx

    for _ in range(2):
        context = build(db, project.project_id)
        assert context["project_instructions"] == "Keep project boundaries.\n"
        assert context["project_id"] == project.project_id
        assert "project_files" not in context
        assert "project_file_count" not in context
    assert calls == []
