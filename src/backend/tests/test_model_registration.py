"""Every ORM table of an installed module is known when the schema is created.

Startup builds or reconciles the database from the metadata that ``load_models`` fills
by importing each installed package's ``models`` module. A table declared anywhere else
and not imported from there is created by Alembic on servers but never on a desktop
database, which has no Alembic.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _declared_tables(package: str) -> dict[str, str]:
    root = BACKEND.joinpath(*package.split("."))
    found = {}
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (isinstance(item, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == "__tablename__" for t in item.targets)
                            and isinstance(item.value, ast.Constant)):
                        found[item.value.value] = str(path.relative_to(BACKEND))
    return found


def test_every_installed_table_is_registered_by_load_models():
    from features.installed import INSTALLED_FEATURES

    script = (
        "import json\n"
        "from features.installed import INSTALLED_FEATURES\n"
        "from core.harness.registry import load_models\n"
        "load_models(INSTALLED_FEATURES)\n"
        "import core.db.models\n"
        "from core.db.engine import Base\n"
        "print(json.dumps(sorted(Base.metadata.tables)))\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(BACKEND), os.environ.get("PYTHONPATH", "")])}
    result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND, env=env,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    registered = set(json.loads(result.stdout.strip().splitlines()[-1]))

    declared = {}
    for package in (*INSTALLED_FEATURES, "core"):
        declared.update(_declared_tables(package))
    missing = {table: where for table, where in declared.items() if table not in registered}
    assert not missing, (
        "Tables declared outside what load_models imports (import them from the package's "
        f"models module): {missing}"
    )
