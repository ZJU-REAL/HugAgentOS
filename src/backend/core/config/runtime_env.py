"""Environment-only paths available before database initialization."""
import os
from pathlib import Path


def local_data_dir() -> Path:
    """本机部署的数据根（``HUGAGENT_HOME``，由桌面壳注入）。

    库、能力文件、备份、快照都挂在这个根下。它是一句 ``os.getenv`` 的事，但散抄
    到各处就会在改默认值时漏掉一处，所以只在这里算一次。
    """
    return Path(os.getenv("HUGAGENT_HOME", str(Path.home() / ".hugagent"))).expanduser()


CAPS_ROOT_ENV = "HUGAGENT_CAPS_ROOT"
