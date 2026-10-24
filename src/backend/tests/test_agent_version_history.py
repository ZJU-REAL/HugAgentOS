"""Agent version helpers retain the base class normalization contract."""
from core.capabilities.agents.base import UserAgentBaseService
from core.capabilities.agents import version_history


def test_version_operations_use_base_class_normalizer(monkeypatch):
    monkeypatch.setattr(UserAgentBaseService, "_normalize_version", staticmethod(lambda value: "V9.9"))
    assert version_history.read_version(UserAgentBaseService, {"version": "old"}) == "V9.9"
    assert version_history.increment_version(UserAgentBaseService, "old") == "V9.10"


def test_history_uses_base_class_detail_normalizer(monkeypatch):
    monkeypatch.setattr(UserAgentBaseService, "_normalize_change_details", staticmethod(lambda value: [{"field": "checked"}]))
    rows = version_history.read_change_history(UserAgentBaseService, {"change_history": [{"timestamp": "now", "content": "change", "version": "V1.0"}]})
    assert rows[0]["details"] == [{"field": "checked"}]
