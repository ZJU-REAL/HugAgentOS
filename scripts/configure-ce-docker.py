"""Initialize a new CE Docker deployment; never overwrite an existing .env."""

import argparse
import os
import re
import secrets
from pathlib import Path


def configure(root: Path, project: str, storage: Path) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
        raise ValueError("Invalid Compose project name")
    root, storage = root.resolve(), storage.resolve()
    template = (root / ".env.example").read_text()
    if not re.search(r"^JX_EDITION=ce$", template, re.M):
        raise ValueError("Run this initializer in the derived CE Docker tree")
    values = {
        "COMPOSE_PROJECT_NAME": project,
        "HOST_REPO_PATH": str(root),
        "HOST_STORAGE_PATH": str(storage),
        "OPENSANDBOX_API_KEY": secrets.token_urlsafe(32),
        "S3_ACCESS_KEY": secrets.token_hex(16),
        "S3_SECRET_KEY": secrets.token_urlsafe(48),
    }
    for name, value in values.items():
        # Compose single quotes prevent interpolation of path dollar signs.
        quoted = "'" + value.replace("'", "\\'") + "'"
        template, count = re.subn(
            rf"^{name}=.*$", lambda _: name + "=" + quoted, template, flags=re.M
        )
        if count != 1:
            raise ValueError(f"CE template must declare {name} exactly once")
    destination = root / ".env"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write(template)
    storage.mkdir(parents=True, exist_ok=True)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", default="hugagent")
    parser.add_argument("--storage", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    configure(root, args.project_name, args.storage or root / "data/storage")
    print("CE Docker configuration initialized. Build the sandbox runtime before startup.")
