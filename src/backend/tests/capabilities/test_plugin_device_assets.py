"""插件在本机需要的资产，跟着插件走而不是跟着本机启动走。

站点插件的建站流程要在沙箱里执行 ``$SITE_TEMPLATE_HOME/init-react-site.sh``。混合模式下
这个插件是从云端账号同步下来的，本机既不引导默认插件、也没有它的安装记录——资产若挂在
"本机引导"上就永远不会铺，本机项目建站会缺文件；挂在"本机启动"上又会给从不建站的机器
凭空铺一堆东西。正确的触发点是"这台机器上有没有这个能力"。
"""

from __future__ import annotations

import pytest
from core.capabilities import device_catalog
from core.services import plugin_device_assets
from core.services.plugin_device_assets import provision_for, provision_present_plugins


@pytest.fixture()
def template_home(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: True)
    home = tmp_path / "site-template"
    monkeypatch.setenv("SITE_TEMPLATE_HOME", str(home))
    return home


@pytest.fixture()
def bare_device(monkeypatch):
    """云端投影为空、本机业务库也没有插件的机器。"""
    monkeypatch.setattr(device_catalog, "plugin_entries", list)

    class _Query:
        def filter(self, *_):
            return self

        def distinct(self):
            return []

    class _Db:
        def query(self, *_):
            return _Query()

    return _Db()


def test_site_plugin_provisions_the_build_template(template_home):
    assert provision_for("sites") is True
    assert (template_home / "init-react-site.sh").is_file()
    assert (template_home / "react-vite").is_dir()


def test_plugins_without_device_assets_touch_nothing(template_home):
    assert provision_for("automation") is True
    assert provision_for("skill-manager") is True
    assert not template_home.exists()


def test_container_deployments_provision_nothing(template_home, monkeypatch):
    """容器部署的沙箱镜像里已经有这些资产，宿主机上不该再铺一份。"""
    monkeypatch.setattr("core.config.local_mode.local_mode_enabled", lambda: False)

    assert provision_for("sites") is True
    assert not template_home.exists()


def test_refresh_covers_a_cloud_synced_plugin_with_no_local_install(
    template_home, bare_device, monkeypatch
):
    """插件只存在于云端投影里时，升级后的资产刷新照样要覆盖它。"""
    monkeypatch.setattr(
        device_catalog, "plugin_entries", lambda: [{"slug": "sites"}, {"slug": "yida"}]
    )

    assert provision_present_plugins(bare_device) == ["sites"]
    assert (template_home / "init-react-site.sh").is_file()


def test_refresh_skips_machines_without_the_capability(template_home, bare_device):
    assert provision_present_plugins(bare_device) == []
    assert not template_home.exists()


def test_missing_bundle_reports_not_ready_instead_of_raising(template_home, monkeypatch):
    """安装包里没有模板时明确报不就绪，不要抛错拦住安装或同步。"""
    monkeypatch.setattr(plugin_device_assets, "_bundled_site_template", lambda: None)

    assert provision_for("sites") is False
    assert not (template_home / "init-react-site.sh").exists()


def test_existing_template_keeps_its_dependencies_and_refreshes_the_script(template_home):
    """已装好 node_modules 的模板目录不被覆盖，init 脚本每次刷新到安装包里的版本。"""
    assert provision_for("sites") is True
    marker = template_home / "react-vite" / "node_modules" / "keep-me"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("x", encoding="utf-8")
    script = template_home / "init-react-site.sh"
    script.write_text("# stale", encoding="utf-8")

    assert provision_for("sites") is True
    assert marker.is_file()
    assert script.read_text(encoding="utf-8") != "# stale"
    assert script.stat().st_mode & 0o111
