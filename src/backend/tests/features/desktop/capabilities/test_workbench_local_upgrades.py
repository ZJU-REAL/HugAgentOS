"""Device upgrade seam using the real immutable local store on an isolated test root."""

from pathlib import Path
import zipfile
import io

from tests.features.workbench.test_workbench_upgrade_routes import upgraded
from tests.features.workbench.workbench_plugin_support import package


def unpack(path, raw):
    path.mkdir()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        archive.extractall(path)
    return str(path.resolve())


def test_local_upgrade_requires_approval_and_preserves_paused_state(client, tmp_path):
    from features.desktop.local.plugins import service as service
    from features.workbench import local_plugin_upgrades
    from features.desktop.capabilities import registry

    old_path = unpack(tmp_path / "old-plugin", package())
    new_path = unpack(tmp_path / "new-plugin", upgraded(grants=["file.save"]))
    installed = service.install("local-u1", old_path)
    install = installed["install_id"]
    previous = installed["revision"]
    service.set_enabled_for_user("local-u1", install, False)
    pending = service.update("local-u1", install, new_path, previous)
    assert pending["action"] == "approval_required"
    assert service.get("local-u1", install)["revision"] == previous
    with registry._session() as db:
        value = local_plugin_upgrades.read(db, "local-u1", install)
    assert value["addedPermissions"] == ["module:page:file.save"]
    result = local_plugin_upgrades.decide(
        "local-u1", install, previous, value["candidateRevision"], "approve", False
    )
    assert result["revision"] != previous
    assert result["enabled"] is False


def test_local_candidates_do_not_materialize_children_and_rejection_reclaims_exact_revision(
    client, tmp_path
):
    from features.desktop.local.plugins import service as service
    from features.workbench import local_plugin_upgrades
    from features.desktop.capabilities import registry, store

    old_path = unpack(tmp_path / "old-candidate-test", package())
    root = tmp_path / "new-candidate-test"
    new_path = unpack(root, upgraded(grants=["file.save"]))
    child = root / "skills" / "new-child"
    child.mkdir(parents=True)
    (child / "SKILL.md").write_text("---\nname: new-child\ndescription: Candidate child\n---\nBody")
    installed = service.install("local-u1", old_path)
    pending = service.update("local-u1", installed["install_id"], new_path, installed["revision"])
    assert pending["action"] == "approval_required"
    assert list(store.iter_components("skill", "local")) == []
    with registry._session() as db:
        value = local_plugin_upgrades.read(db, "local-u1", installed["install_id"])
    candidate = value["candidateRevision"]
    local_plugin_upgrades.decide(
        "local-u1", installed["install_id"], installed["revision"], candidate, "reject", False
    )
    assert store.get("plugin", "local", installed["slug"], candidate) is None
    assert service.get("local-u1", installed["install_id"])["revision"] == installed["revision"]
    with registry._session() as db:
        assert (
            local_plugin_upgrades.read(db, "local-u1", installed["install_id"])["candidateStorage"][
                "count"
            ]
            == 0
        )
