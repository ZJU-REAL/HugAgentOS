"""Resolve declared non-secret runtime settings at the installation boundary."""
import os
import re

def runtime_configuration(resource):
    config = {}
    for key, spec in (resource.get("configuration") or {}).items():
        if not isinstance(spec, dict) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            raise ValueError("invalid_runtime_configuration")
        env = spec.get("env", "")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env) or re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY", env):
            raise ValueError("secret_environment_not_allowed")
        value = os.getenv(env)
        kind = spec.get("type", "string")
        if value is None:
            value = spec.get("default")
        elif kind == "boolean":
            if value.lower() not in {"true", "false"}:
                raise ValueError("invalid_boolean_configuration")
            value = value.lower() == "true"
        elif kind == "array":
            value = [x.strip() for x in value.split(",") if x.strip()]
        elif kind == "integer":
            value = int(value)
        elif kind != "string":
            raise ValueError("unsupported_configuration_type")
        expected = {"string": str, "boolean": bool, "array": list, "integer": int}.get(kind)
        if expected is None or not isinstance(value, expected) or (kind == "integer" and isinstance(value, bool)):
            raise ValueError("invalid_runtime_configuration_type")
        if kind == "array" and not all(isinstance(item, str) for item in value):
            raise ValueError("invalid_array_configuration")
        config[key] = value
    return config
