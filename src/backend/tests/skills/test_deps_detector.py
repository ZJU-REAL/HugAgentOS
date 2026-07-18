"""Tests for agent_skills.deps_detector."""
from __future__ import annotations

import pytest

from core.agent_skills.deps_detector import detect_dependencies


def _names(deps: list[dict]) -> list[str]:
    return [d["name"] for d in deps]


def test_empty_input() -> None:
    result = detect_dependencies({})
    assert result == {"pip": [], "npm": [], "apt": [], "warnings": []}
    assert detect_dependencies(None) == result


def test_requirements_txt_parsing() -> None:
    files = {
        "requirements.txt": (
            "# comment line\n"
            "pandas>=1.5\n"
            "numpy==1.24.0\n"
            "requests\n"
            "openpyxl[xlsx]>=3.0 ; python_version >= '3.8'\n"
            "-e git+https://github.com/foo/bar  # ignored\n"
            "\n"
        ),
    }
    result = detect_dependencies(files)
    pip_by_name = {d["name"]: d for d in result["pip"]}
    assert pip_by_name["pandas"]["version"] == ">=1.5"
    assert pip_by_name["pandas"]["source"] == "requirements.txt"
    assert pip_by_name["numpy"]["version"] == "==1.24.0"
    assert pip_by_name["requests"]["version"] is None
    assert pip_by_name["openpyxl"]["version"] == ">=3.0"
    # `-e ...` is ignored
    assert "bar" not in pip_by_name


def test_pyproject_toml_pep621() -> None:
    files = {
        "pyproject.toml": (
            "[project]\n"
            'name = "demo"\n'
            'dependencies = ["pandas>=1.0", "requests"]\n'
        ),
    }
    result = detect_dependencies(files)
    pip_by_name = {d["name"]: d for d in result["pip"]}
    assert pip_by_name["pandas"]["source"] == "pyproject.toml"
    assert pip_by_name["pandas"]["version"] == ">=1.0"


def test_pyproject_toml_poetry() -> None:
    files = {
        "pyproject.toml": (
            "[tool.poetry.dependencies]\n"
            'python = "^3.11"\n'
            'pandas = "^1.5"\n'
            'requests = {version = "^2.0"}\n'
        ),
    }
    result = detect_dependencies(files)
    pip_by_name = {d["name"]: d for d in result["pip"]}
    assert "pandas" in pip_by_name
    assert pip_by_name["pandas"]["source"] == "pyproject.toml"
    assert "python" not in pip_by_name


def test_package_json() -> None:
    files = {
        "package.json": (
            '{\n'
            '  "dependencies": {"pptxgenjs": "^3.0", "lodash": "*"},\n'
            '  "devDependencies": {"vite": "^4.0"}\n'
            '}\n'
        ),
    }
    result = detect_dependencies(files)
    npm_by_name = {d["name"]: d for d in result["npm"]}
    assert npm_by_name["pptxgenjs"]["version"] == "^3.0"
    assert npm_by_name["pptxgenjs"]["source"] == "package.json"
    assert npm_by_name["lodash"]["version"] is None  # "*" → None
    assert "vite" not in npm_by_name  # devDependencies ignored


def test_apt_requirements() -> None:
    files = {"apt-requirements.txt": "pandoc\n# heavy\nlibreoffice\n"}
    result = detect_dependencies(files)
    assert _names(result["apt"]) == ["libreoffice", "pandoc"]
    assert all(d["source"] == "apt-requirements.txt" for d in result["apt"])


def test_static_import_scan_basic() -> None:
    files = {
        "main.py": "import pandas\nimport numpy as np\nfrom os import path\n",
    }
    result = detect_dependencies(files)
    names = _names(result["pip"])
    assert "pandas" in names
    assert "numpy" in names
    assert "os" not in names  # stdlib filtered


def test_static_import_name_mapping() -> None:
    files = {
        "script.py": (
            "import cv2\n"
            "from PIL import Image\n"
            "import yaml\n"
            "import sklearn.linear_model\n"
            "import docx\n"
        ),
    }
    result = detect_dependencies(files)
    names = _names(result["pip"])
    assert "opencv-python" in names
    assert "Pillow" in names
    assert "PyYAML" in names
    assert "scikit-learn" in names
    assert "python-docx" in names
    # Original import names should NOT appear
    assert "cv2" not in names and "PIL" not in names and "docx" not in names


def test_intra_skill_modules_skipped() -> None:
    files = {
        "helper.py": "def f(): pass\n",
        "main.py": "from helper import f\nimport pandas\n",
    }
    result = detect_dependencies(files)
    names = _names(result["pip"])
    assert "pandas" in names
    assert "helper" not in names


def test_relative_imports_skipped() -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/main.py": "from . import sub\nfrom ..util import helper\nimport pandas\n",
    }
    result = detect_dependencies(files)
    names = _names(result["pip"])
    assert "pandas" in names
    assert "sub" not in names
    assert "util" not in names


def test_syntax_error_produces_warning() -> None:
    files = {"bad.py": "def f(:\n  pass\n"}
    result = detect_dependencies(files)
    assert any("bad.py" in w and "解析失败" in w for w in result["warnings"])


def test_dynamic_import_warning() -> None:
    files = {
        "dyn.py": (
            "import importlib\n"
            "mod = importlib.import_module('some_pkg')\n"
        ),
    }
    result = detect_dependencies(files)
    assert any("dyn.py" in w and "importlib" in w for w in result["warnings"])


def test_dedup_prefers_manifest_over_static() -> None:
    files = {
        "requirements.txt": "pandas>=1.5\n",
        "main.py": "import pandas\nimport numpy\n",
    }
    result = detect_dependencies(files)
    pip_by_name = {d["name"]: d for d in result["pip"]}
    # requirements.txt wins for pandas
    assert pip_by_name["pandas"]["source"] == "requirements.txt"
    assert pip_by_name["pandas"]["version"] == ">=1.5"
    # numpy only found via static scan
    assert pip_by_name["numpy"]["source"] == "static_scan"


def test_results_sorted_by_name() -> None:
    files = {
        "main.py": "import zlib_helper\nimport apple\nimport banana\n",
    }
    result = detect_dependencies(files)
    names = _names(result["pip"])
    assert names == sorted(names, key=str.lower)


def test_intra_skill_subpackage_not_flagged() -> None:
    """A skill's own subpackage (e.g. scripts/core/) must not be misjudged as a PyPI dependency when imported."""
    files = {
        "requirements.txt": "openpyxl>=3.0.0\n",
        "scripts/main.py": "import sys\nfrom core.parser import parse\nimport openpyxl\n",
        "scripts/core/__init__.py": "",
        "scripts/core/parser.py": "from core.saver import save\n",
        "scripts/core/saver.py": "import json\n",
    }
    names = _names(detect_dependencies(files)["pip"])
    assert "core" not in names          # subpackage, not a dependency
    assert "scripts" not in names       # directory name, not a dependency
    assert "openpyxl" in names


def test_test_files_excluded_from_static_scan() -> None:
    """Imports inside test files (pytest, etc.) are not runtime dependencies and should be excluded."""
    files = {
        "requirements.txt": "pandas>=1.3.0\n",
        "scripts/app.py": "import pandas\n",
        "tests/test_app.py": "import pytest\nimport responses\n",
        "tests/conftest.py": "import hypothesis\n",
        "scripts/helpers_test.py": "import pytest\n",
    }
    names = _names(detect_dependencies(files)["pip"])
    assert "pytest" not in names
    assert "responses" not in names
    assert "hypothesis" not in names
    assert "pandas" in names
