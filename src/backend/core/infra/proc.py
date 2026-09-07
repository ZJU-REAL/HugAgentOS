"""子进程启动的平台化参数。

桌面本机模式下后端由 GUI 外壳拉起、自身没有控制台。此时再启动控制台程序
（libreoffice、pandoc、cmd、连接器 CLI 等），Windows 会为它新开一个黑色 cmd
窗口。所有可能在宿主机上执行的子进程都要带上这里的参数；容器部署里
``os.name`` 不是 ``nt``，返回空字典、行为不变。
"""

import os
import subprocess
from typing import Any, Dict

__all__ = ["no_window_kwargs"]


def no_window_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
