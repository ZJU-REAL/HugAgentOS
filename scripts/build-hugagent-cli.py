"""Build the lightweight client wheel without platform server dependencies."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
args.out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="hugagent-cli-") as temporary:
    base = Path(temporary)
    shutil.copytree(
        root / "src/backend/hugagent_cli",
        base / "hugagent_cli",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (base / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=68", "wheel"]\nbuild-backend = "setuptools.build_meta"\n[project]\nname = "hugagent-cli"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndescription = "HugAgentOS cloud and device session client"\n[project.scripts]\nhugagent-session = "hugagent_cli.main:main"\n'
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(args.out.resolve()),
            str(base),
        ],
        check=True,
    )
