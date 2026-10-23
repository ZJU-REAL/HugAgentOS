"""Render the controller TOML from explicit Docker deployment settings."""

import json
import os
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath


def render(template, env):
    values = {}
    for name in ("HOST_REPO_PATH", "HOST_STORAGE_PATH"):
        value = env[name].rstrip("/")
        if not PurePosixPath(value).is_absolute() or value == "":
            raise ValueError(f"{name} must be an absolute host path")
        values[name] = value
    project = env["COMPOSE_PROJECT_NAME"]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError("COMPOSE_PROJECT_NAME must be an explicit Compose project name")
    values["COMPOSE_PROJECT_NAME"] = project
    values["OPENSANDBOX_API_KEY"] = env.get("OPENSANDBOX_API_KEY", "")
    for name, value in values.items():
        template = template.replace("@@" + name + "@@", json.dumps(value)[1:-1])
    if "@@" in template:
        raise ValueError("Unresolved OpenSandbox configuration placeholder")
    tomllib.loads(template)
    return template


if __name__ == "__main__":
    source, destination = map(Path, sys.argv[1:])
    destination.write_text(render(source.read_text(), os.environ))
