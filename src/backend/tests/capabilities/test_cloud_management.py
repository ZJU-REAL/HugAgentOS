"""Cloud lifecycle: ownership, revision checks and reinstall after removal."""
from contextlib import contextmanager
import pytest
from core.db.models import AdminSkill, AdminMcpServer, InstalledPlugin
from core.services import cloud_management as management, capability_workcopies as copies


@pytest.fixture
def cloud_db(index_db, monkeypatch):
    from core.db.engine import Base
    Base.metadata.create_all(index_db.kw["bind"])
    monkeypatch.delenv("HUGAGENT_CAPS_ROOT", raising=False)
    monkeypatch.setattr(management, "SessionLocal", index_db)
    monkeypatch.setattr(copies, "SessionLocal", index_db)
    monkeypatch.setattr("api.routes.v1.me_capabilities._require_flag", lambda *a: None)
    monkeypatch.setattr(management, "_permission", lambda *a: None)
    monkeypatch.setattr("core.ontology.build_validator.ensure_ontology_build_valid", lambda *a, **kw: None)
    monkeypatch.setattr("core.agent_skills.cache_refresh.refresh_skill_caches", lambda: None)
    return index_db


def test_cloud_skill_install_update_remove_reinstall(cloud_db, tmp_path, monkeypatch):
    folder = tmp_path / "skill"
    folder.mkdir()
    md = folder / "SKILL.md"
    md.write_text("---\nname: cloud-example\ndescription: Example\n---\nFirst")
    @contextmanager
    def package(user, source):
        yield folder
    monkeypatch.setattr(management, "artifact_package", package)
    installed = management.install("owner", "skill", {})
    assert management.install("owner", "skill", {})["revision"] == installed["revision"]
    with pytest.raises(Exception):
        management.get("other", "skill", installed["install_id"])
    md.write_text("---\nname: cloud-example\ndescription: Example\n---\nSecond")
    updated = management.update("owner", "skill", installed["install_id"], {}, installed["revision"])
    with pytest.raises(ValueError, match="revision_conflict"):
        management.uninstall("owner", "skill", installed["install_id"], installed["revision"])
    management.uninstall("owner", "skill", installed["install_id"], updated["revision"])
    reinstalled = management.install("owner", "skill", {})
    assert reinstalled["revision"] == updated["revision"]
    with cloud_db() as db:
        assert db.get(AdminSkill, installed["install_id"]) is not None


def test_cloud_plugin_revision_includes_child_content(cloud_db):
    with cloud_db() as db:
        db.add(InstalledPlugin(install_id="bundle@owner", slug="bundle", name="Bundle", owner_user_id="owner", component_ids={"skills": ["child"]}))
        db.add(AdminSkill(skill_id="child", display_name="Child", description="Child", skill_content="First", owner_user_id="owner", source_plugin="bundle"))
        db.commit()
    first = management.get("owner", "plugin", "bundle@owner")
    with cloud_db() as db:
        db.get(AdminSkill, "child").skill_content = "Changed independently"
        db.commit()
    second = management.get("owner", "plugin", "bundle@owner")
    assert first["revision"] != second["revision"]
    with pytest.raises(ValueError, match="revision_conflict"):
        management.uninstall("owner", "plugin", "bundle@owner", first["revision"])


def test_cloud_artifact_owned_bytes_reach_installer(cloud_db, tmp_path, monkeypatch):
    import io
    import zipfile
    from core.db.models import Artifact
    from core.storage.local import LocalStorageBackend
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as package:
        package.writestr("SKILL.md", "---\nname: owned-artifact\ndescription: Artifact example\n---\nBody")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    storage = LocalStorageBackend()
    storage.upload_bytes(data.getvalue(), "packages/skill.zip")
    monkeypatch.setattr("core.storage.get_storage", lambda: storage)
    with cloud_db() as db:
        db.add(Artifact(artifact_id="owned-package", user_id="owner", type="other", title="Package", filename="skill.zip",
            size_bytes=len(data.getvalue()), mime_type="application/zip", storage_key="packages/skill.zip"))
        db.commit()
    source = {"kind": "artifact", "artifact_id": "owned-package"}
    with pytest.raises(PermissionError):
        management.install("other", "skill", source)
    result = management.install("owner", "skill", source)
    assert result["ok"] and result["source"] == "cloud"


def test_official_manager_upgrade_replaces_retired_tools(cloud_db, monkeypatch):
    from core.services import plugin_service, manager_bundle_upgrade
    monkeypatch.setattr(plugin_service, "_refresh_after_change", lambda *_: None)
    with cloud_db() as db:
        plugin_service.install_plugin(db, "skill-manager", owner_user_id="owner")
        row = db.query(InstalledPlugin).filter_by(slug="skill-manager").one()
        row.version = "1.0.0"
        server = db.query(AdminMcpServer).filter_by(source_plugin="skill-manager").one()
        server.tools_json = [{"name": "register_skill", "inputSchema": {"type": "object"}}]
        db.commit()
        assert manager_bundle_upgrade.refresh(db) == 1
        db.expire_all()
        server = db.query(AdminMcpServer).filter_by(source_plugin="skill-manager").one()
        names = {tool["name"] for tool in server.tools_json}
        assert "install_skill" in names and "register_skill" not in names
        install = next(t for t in server.tools_json if t["name"] == "install_skill")
        assert install["_meta"]["org.hugagent/executor"]["version"] == 1
        assert manager_bundle_upgrade.refresh(db) == 0
