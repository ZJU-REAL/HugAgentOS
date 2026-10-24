from types import SimpleNamespace as NS
import pytest
from fastapi import HTTPException
from core.files.artifacts.location import artifact_location
from api.routes.file_location import file_location


def folder(name, parent=None, owner="owner", deleted=None):
    return NS(name=name, parent_folder_id=parent, user_id=owner, deleted_at=deleted)


def artifact(folder_id="leaf"):
    return NS(
        user_id="owner",
        user_folder_id=folder_id,
        filename="报告.xlsx",
        title="报告",
        deleted_at=None,
    )


def database(folders):
    return NS(get=lambda model, key: folders.get(key))


def test_personal_location_uses_folder_names_not_storage():
    result = artifact_location(
        database({"leaf": folder("项目", "parent"), "parent": folder("资料")}), artifact()
    )
    assert result == {"kind": "personal", "root_name": None, "relative_path": "资料/项目/报告.xlsx"}


def test_root_and_unfiled_are_distinct():
    assert artifact_location(database({}), artifact(None))["relative_path"] == "报告.xlsx"
    assert artifact_location(database({}), None)["kind"] == "unfiled"


@pytest.mark.parametrize(
    "folders,status",
    [
        ({"leaf": folder("私密", owner="other")}, 404),
        ({"leaf": folder("已删除", deleted=True)}, 404),
        ({"leaf": folder("循环", "leaf")}, 409),
        ({}, 404),
    ],
)
def test_invalid_ancestry_never_leaks_names(folders, status):
    with pytest.raises(HTTPException) as error:
        artifact_location(database(folders), artifact())
    assert error.value.status_code == status


def test_location_requires_file_authorization_before_folder_lookup(monkeypatch):
    monkeypatch.setattr(
        "api.routes.files._load_artifact_item", lambda *args: {"metadata": {"user_id": "other"}}
    )

    def denied(**kwargs):
        raise HTTPException(403, "Denied")

    monkeypatch.setattr("api.routes.files._authorize_access", denied)
    with pytest.raises(HTTPException) as error:
        file_location(
            "private", NS(user_id="owner"), NS(get=lambda *args: pytest.fail("Unauthorized query"))
        )
    assert error.value.status_code == 403


def test_deleted_artifact_location_is_hidden():
    item = artifact()
    item.deleted_at = True
    with pytest.raises(HTTPException) as error:
        artifact_location(database({}), item)
    assert error.value.status_code == 404
