"""Desktop local-mode gate (edition-agnostic).

``local_mode_enabled()`` is the single guard for every desktop local capability
— local folder projects, folder grants, the execution policy gate. It is True
only under the Docker-free single-machine deployment profile
(``DEPLOY_PROFILE=local``), so a regular compose / cloud / web deployment never
exposes any of these routes or behaviors. This module lives in shared ``core``
so it is present in both the EE tree and the CE derivation, but stays inert
(returns False) everywhere except the desktop local backend.
"""

from __future__ import annotations


def local_mode_enabled() -> bool:
    try:
        from core.config.settings import settings

        return bool(settings.deploy.is_local)
    except Exception:
        return False


__all__ = ["local_mode_enabled"]
