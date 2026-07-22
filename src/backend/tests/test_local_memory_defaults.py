"""Local one-command profile memory defaults."""

import io
import os
from pathlib import Path
from types import SimpleNamespace

import cli
import pytest


def test_local_profile_enables_memory_runtime_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HUGAGENT_HOME", str(tmp_path / "hugagent-home"))
    monkeypatch.delenv("MEM0_ENABLED", raising=False)
    monkeypatch.delenv("HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS", raising=False)

    defaults = cli.apply_local_env(port=18000)

    assert defaults["MEM0_ENABLED"] == "true"
    assert defaults["HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS"] == "1"
    assert os.environ["HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS"] == "1"


def test_local_bootstrap_installs_recommended_plugins_only_once(tmp_path, monkeypatch):
    home = tmp_path / "hugagent-home"
    monkeypatch.setenv("HUGAGENT_HOME", str(home))
    cli.apply_local_env(port=18000)
    installs = []
    provisions = []

    def fake_install(slugs):
        installs.append(list(slugs))
        return list(slugs)

    monkeypatch.setattr(cli, "install_plugins", fake_install)
    monkeypatch.setattr(
        cli,
        "provision_site_template",
        lambda verbose=False: provisions.append(verbose) or True,
    )

    assert cli.ensure_default_plugins_once() is True
    assert cli.ensure_default_plugins_once() is False
    assert installs == [["automation", "skill-manager", "sites"]]
    assert provisions == [True, False]
    assert (home / ".default-plugins-v1").read_text(encoding="utf-8").splitlines() == [
        "automation",
        "skill-manager",
        "sites",
    ]


def test_local_bootstrap_retries_after_partial_plugin_failure(tmp_path, monkeypatch):
    home = tmp_path / "hugagent-home"
    monkeypatch.setenv("HUGAGENT_HOME", str(home))
    cli.apply_local_env(port=18000)
    monkeypatch.setattr(cli, "install_plugins", lambda _slugs: ["automation", "skill-manager"])

    with pytest.raises(RuntimeError, match="sites"):
        cli.ensure_default_plugins_once()

    assert not (home / ".default-plugins-v1").exists()


def test_local_serve_fails_readiness_when_default_plugin_bootstrap_fails(monkeypatch):
    monkeypatch.setenv("HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS", "1")
    monkeypatch.setattr(cli, "apply_local_env", lambda _port: {})
    monkeypatch.setattr(cli, "_ensure_schema_and_seed", lambda: None)

    def fail_bootstrap():
        raise RuntimeError("sites missing")

    monkeypatch.setattr(cli, "ensure_default_plugins_once", fail_bootstrap)

    with pytest.raises(RuntimeError, match="sites missing"):
        cli.cmd_serve(
            SimpleNamespace(
                port=18000,
                host="127.0.0.1",
                no_browser=True,
            )
        )


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


def test_ce_one_command_installer_bootstraps_default_plugins():
    repo_root = Path(__file__).resolve().parents[3]
    installer_path = repo_root / "ce" / "overlay" / "install.sh"
    if not installer_path.is_file():
        installer_path = repo_root / "install.sh"

    installer = installer_path.read_text(encoding="utf-8")

    assert "export HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS=1" in installer


def test_desktop_local_server_bootstraps_default_plugins():
    repo_root = Path(__file__).resolve().parents[3]
    launcher = (repo_root / "desktop" / "src-tauri" / "src" / "local_server.rs").read_text(
        encoding="utf-8"
    )

    assert '.env("HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS", "1")' in launcher


def test_desktop_local_server_forces_utf8_python_stdio():
    repo_root = Path(__file__).resolve().parents[3]
    launcher = (repo_root / "desktop" / "src-tauri" / "src" / "local_server.rs").read_text(
        encoding="utf-8"
    )

    assert '.env("PYTHONUTF8", "1")' in launcher
    assert '.env("PYTHONIOENCODING", "utf-8")' in launcher


def test_status_output_does_not_fail_on_gbk_redirect(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
    monkeypatch.setattr(cli.sys, "stdout", stream)

    cli._status("  ✓ 已安装插件：sites")
    stream.flush()

    assert "已安装插件：sites" in raw.getvalue().decode("gbk")


def test_cli_reconfigures_redirected_gbk_stream_to_utf8(monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="gbk", errors="strict")
    monkeypatch.setattr(cli.sys, "stdout", stream)

    cli._configure_standard_streams()
    cli._status("✓ 默认插件已就绪")
    stream.flush()

    assert raw.getvalue().decode("utf-8") == "✓ 默认插件已就绪\n"


@pytest.mark.parametrize(
    "relative_path",
    [
        "desktop/resources/server-bootstrap/install-local-server.ps1",
        "desktop/resources/server-bootstrap/install-local-server.sh",
    ],
)
def test_desktop_installer_includes_persistent_memory_runtime(relative_path):
    repo_root = Path(__file__).resolve().parents[3]
    installer = (repo_root / relative_path).read_text(encoding="utf-8-sig")

    assert "requirements-mem0.txt" in installer
    assert "protobuf<7" in installer
    assert "pymilvus==2.5.18" in installer
    assert "milvus-lite==3.1.0" in installer
