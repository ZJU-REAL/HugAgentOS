"""Dropped skill folders enter the same device listing used by cards."""

import os
from pathlib import Path

from tests.capabilities.test_desktop_capabilities_api import client


def test_dropped_skill_appears_once_and_refreshes_after_edit(client):
    folder = Path(os.environ["HUGAGENT_CAPS_ROOT"]) / "skills" / "dropped"
    folder.mkdir(parents=True)
    folder.joinpath("SKILL.md").write_text(
        "---\nname: dropped\ndescription: A dropped skill\n---\nFirst version"
    )
    result = client.get("/v1/desktop/capabilities/installations?kind=skill")
    assert result.status_code == 200
    matches = [item for item in result.json()["data"]["items"] if item["runtime_name"] == "dropped"]
    assert len(matches) == 1
    assert matches[0]["source"] == "local"
    folder.joinpath("query.json").write_text("{}")
    again = client.get("/v1/desktop/capabilities/installations?kind=skill")
    updated = next(
        item for item in again.json()["data"]["items"] if item["runtime_name"] == "dropped"
    )
    assert updated["revision"] != matches[0]["revision"]
    assert (
        len([item for item in again.json()["data"]["items"] if item["runtime_name"] == "dropped"])
        == 1
    )


def test_duplicate_id_and_invalid_folder_do_not_overwrite_or_break_listing(client):
    root = Path(os.environ["HUGAGENT_CAPS_ROOT"]) / "skills"
    for name in ("first", "second", "broken"):
        folder = root / name
        folder.mkdir(parents=True)
        folder.joinpath("SKILL.md").write_text(
            "---\nname: same\ndescription: News\n---\n" + name if name != "broken" else "invalid"
        )
    response = client.get("/v1/desktop/capabilities/installations").json()["data"]
    assert len([item for item in response["items"] if item["runtime_name"] == "same"]) == 1
    errors = {item["folder"]: item["code"] for item in response["discovery_errors"]}
    assert errors["second"] == "name_conflict"
    assert "broken" in errors
    assert root.joinpath("first", "SKILL.md").read_text().endswith("first")
