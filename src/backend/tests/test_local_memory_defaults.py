"""Local one-command profile memory defaults."""

from pathlib import Path

import cli


def test_local_profile_enables_memory_runtime_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "hugagent-home"))
    monkeypatch.delenv("MEM0_ENABLED", raising=False)

    defaults = cli.apply_local_env(port=18000)

    assert defaults["MEM0_ENABLED"] == "true"


def test_ce_installer_pins_compatible_milvus_lite_stack():
    repo_root = Path(__file__).resolve().parents[3]
    installer_path = repo_root / "ce" / "overlay" / "install.sh"
    if not installer_path.is_file():
        # In the generated public CE tree the overlay becomes the root installer.
        installer_path = repo_root / "install.sh"

    installer = installer_path.read_text(encoding="utf-8")
    requirements = (repo_root / "requirements.txt").read_text(encoding="utf-8")

    assert '"pymilvus==2.5.18"' in installer
    assert '"milvus-lite==3.1.0"' in installer
    assert '"protobuf<7"' in installer
    assert "pymilvus>=2.5.0,<2.6.0" in requirements
    assert "pymilvus[milvus-lite]>=2.5.0" not in installer
