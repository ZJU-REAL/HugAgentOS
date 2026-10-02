"""Loose folders are pending inputs, never implicitly installed by read APIs."""

import os
from pathlib import Path
from core.capabilities import discovery, registry
from core.services import local_skill_service


def test_directory_requires_explicit_install_and_uninstall_never_resurrects(
    client, index_db, monkeypatch
):
    from core.db.engine import Base

    Base.metadata.create_all(index_db.kw["bind"])
    monkeypatch.setattr("core.db.engine.SessionLocal", index_db)
    folder = Path(os.environ["HUGAGENT_CAPS_ROOT"]) / "skills" / "dropped"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: dropped\ndescription: Dropped skill\n---\nBody")
    pending = discovery.scan("local-u1")
    assert any(x["code"] == "pending_import" for x in pending)
    assert not registry.list_installations(kind="skill", profile_id="local")
    installed = local_skill_service.install("local-u1", str(folder))
    local_skill_service.uninstall("local-u1", installed["install_id"], installed["revision"])
    (folder / "SKILL.md").write_text(
        "---\nname: dropped\ndescription: Changed after removal\n---\nBody"
    )
    response = client.get("/v1/desktop/capabilities/installations?kind=skill")
    assert response.status_code == 200
    assert registry.get(installed["install_id"]).state == "removed"
    assert folder.exists()
