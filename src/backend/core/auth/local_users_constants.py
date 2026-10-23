"""Validation patterns and CE bootstrap constants for local accounts."""
from __future__ import annotations
import re
from typing import Any, Dict

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{2,32}$")


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


CE_DEFAULT_ADMIN_USERNAME = "admin"


CE_DEFAULT_ADMIN_PASSWORD = "admin"


CE_ONBOARDING_VERSION = 1


CE_DEFAULT_ADMIN_CAPABILITIES: Dict[str, Any] = {
    "role": "super_admin",
    "ce_default_admin": True,
    "lab_enabled": True,
    "can_use_api_key": True,
    "can_add_skill": True,
    "can_add_mcp": True,
    "can_import_plugin": True,
    "can_add_agent": True,
    "can_create_private_kb": True,
    "can_create_public_kb": False,
    "can_create_channel_bot": True,
    "can_switch_model": True,
    "can_use_ontology_validation": True,
    "can_run_autonomous_loop": True,
    "can_manage_chat_modes": False,
    "can_system_config": True,
    "can_content_manage": True,
    "allowed_apps": "*",
}
