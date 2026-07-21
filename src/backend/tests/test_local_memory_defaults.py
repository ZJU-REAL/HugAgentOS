"""Local one-command profile memory defaults."""

import cli


def test_local_profile_enables_memory_runtime_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "hugagent-home"))
    monkeypatch.delenv("MEM0_ENABLED", raising=False)

    defaults = cli.apply_local_env(port=18000)

    assert defaults["MEM0_ENABLED"] == "true"
