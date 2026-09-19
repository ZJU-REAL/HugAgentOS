"""插件落到这台机器上时要一并就位的本机资产。

有的插件的能力不止是技能正文和连接器，还要在本机放一份文件：站点插件的建站流程会在
沙箱里执行 ``$SITE_TEMPLATE_HOME/init-react-site.sh``，Docker 部署把它烤进沙箱镜像的
``/opt/site-template``，本机没有那一层，得自己铺。

这份资产属于插件，不属于"本机启动"：铺不铺看这台机器上有没有这个能力，而不是看本机
引导跑没跑过。能力有三种落法——本机安装、云端同步准备、以及安装包升级后刷新既有安装
——三条路都从这张表取同一个动作，新增一个带本机资产的插件只需在表里加一行。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _bundled_site_template() -> Optional[Path]:
    """安装包里的 React 站点模板（``docker/site-template/``）。"""
    # src/backend/core/services/plugin_device_assets.py → parents[4] = 仓库根
    candidate = Path(__file__).resolve().parents[4] / "docker" / "site-template"
    return candidate if (candidate / "init-react-site.sh").is_file() else None


def site_template_home() -> Path:
    from core.config.runtime_env import local_data_dir

    return Path(os.environ.get("SITE_TEMPLATE_HOME", str(local_data_dir() / "site-template")))


def provision_site_template() -> bool:
    """把建站模板铺到 ``SITE_TEMPLATE_HOME``；已就绪时只刷新 init 脚本。

    ``node_modules`` 由 init 脚本首次构建时自愈安装，这里不铺，装机才不会变慢；已经
    装好依赖的模板目录也就不会被覆盖。init 脚本每次都刷新，桌面端升级带来的兼容性
    修复才跟得上。
    """
    source = _bundled_site_template()
    if source is None:
        logger.warning("[device-assets] 安装包内未找到站点模板（docker/site-template/）")
        return False
    home = site_template_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        if not (home / "init-react-site.sh").is_file():
            shutil.copytree(
                source / "react-vite",
                home / "react-vite",
                ignore=shutil.ignore_patterns("node_modules"),
                dirs_exist_ok=True,
            )
        shutil.copy2(source / "init-react-site.sh", home / "init-react-site.sh")
        os.chmod(home / "init-react-site.sh", 0o755)
    except OSError as exc:
        logger.warning("[device-assets] 站点模板铺入失败：%s", exc)
        return False
    return True


# 插件 slug → 它在本机需要的资产。值是动作本身，返回是否就绪。
_PROVISIONERS: Dict[str, Callable[[], bool]] = {"sites": provision_site_template}


def provision_for(slug: str) -> bool:
    """铺这个插件声明的本机资产，返回是否就绪（没声明 / 无需铺也算就绪）。

    只有本机部署要铺：容器部署把这些资产烤进了沙箱镜像（站点模板在
    ``/opt/site-template``），宿主机上再铺一份既多余又会写到不属于它的目录。
    """
    from core.config.local_mode import local_mode_enabled

    provision = _PROVISIONERS.get(str(slug or ""))
    if provision is None or not local_mode_enabled():
        return True
    ready = provision()
    if not ready:
        logger.warning("[device-assets] 插件 %s 的本机资产未就绪", slug)
    return ready


def provision_present_plugins(db) -> List[str]:
    """给这台机器上已有的插件补齐本机资产，返回就绪的 slug。

    覆盖的是"能力早就在了、安装包却升级了"的情形：插件本身没有变化，不会触发安装，
    也不会触发云端重新准备，但随版本发出的资产需要跟着更新。本机业务库的行和云端
    同步下来的投影都算"这台机器上有"。
    """
    from core.capabilities import device_catalog
    from core.db.models import InstalledPlugin

    # 只问登记表里那几个 slug 在不在，不把整张表和整份云端投影都物化出来。
    present = {
        row[0]
        for row in db.query(InstalledPlugin.slug)
        .filter(InstalledPlugin.slug.in_(sorted(_PROVISIONERS)))
        .distinct()
    }
    present.update(
        entry["slug"] for entry in device_catalog.plugin_entries() if entry["slug"] in _PROVISIONERS
    )
    return [slug for slug in sorted(present) if provision_for(slug)]
