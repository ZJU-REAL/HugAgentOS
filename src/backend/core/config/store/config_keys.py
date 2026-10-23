"""Map environment variables to the system-config keys that override them."""
from __future__ import annotations
from typing import Optional
from core.config.store import system_config_constants

def get_config_key_for_env(env_var: str) -> Optional[str]:
    """Return the system-config key that backs ``env_var``, if any.

    Public lookup helper so callers (e.g. ``runtime_env``) don't need to import
    the internal reverse map.
    """
    return system_config_constants.ENV_TO_CONFIG_KEY.get(env_var)
