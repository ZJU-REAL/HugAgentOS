import pytest
from core.capabilities.change_merge import plan, resolve, sensitive_paths, validate_files
from core.capabilities.errors import CapabilityError


def test_disjoint_changes_merge_and_keep_outputs():
    base = {"SKILL.md": "base", "old.log": "log"}
    local = {**base, "query.json": "query"}
    cloud = {**base, "SKILL.md": "cloud"}
    assert resolve(base, local, cloud, {}) == {**cloud, "query.json": "query"}


def test_delete_modify_conflict_requires_choice():
    base, local, cloud = {"a": "base"}, {}, {"a": "cloud"}
    assert plan(base, local, cloud)["changes"][0]["conflict"]
    with pytest.raises(ValueError):
        resolve(base, local, cloud, {})
    assert resolve(base, local, cloud, {"a": {"side": "local"}}) == {}
    assert resolve(base, local, cloud, {"a": {"side": "cloud"}}) == cloud


def test_keep_both_manual_and_collision():
    assert resolve(
        {"a": "0"}, {"a": "1"}, {"a": "2"}, {"a": {"side": "both", "alternate_path": "a-local"}}
    ) == {"a": "2", "a-local": "1"}
    assert resolve(
        None, {"a": "1"}, {"a": "2"}, {"a": {"side": "manual", "content": "merged"}}
    ) == {"a": "merged"}
    with pytest.raises((ValueError, CapabilityError)):
        resolve(None, {"a": "1"}, {"a": "2"}, {"a": {"side": "both", "alternate_path": "../a"}})


@pytest.mark.parametrize("name", ["../x", "/x", "a/../x", "NUL", "a\\b"])
def test_invalid_paths_rejected(name):
    with pytest.raises((ValueError, CapabilityError)):
        validate_files({name: "content"})


def test_json_secret_warning_does_not_remove_files():
    files = {"query.json": '{"token": "example"}', "run.log": "ordinary"}
    assert sensitive_paths(files) == ["query.json"]
    assert validate_files(files) == files
