"""Detect runtime dependencies (pip / npm / apt) from a skill's bundled files.

Two-layer detection:
  1. Parse declared manifests (`requirements*.txt`, `pyproject.toml`, `package.json`,
     `apt-requirements.txt` / `Aptfile`).
  2. Static AST scan of `.py` files: extract top-level `import` / `from ... import`,
     filter out stdlib + intra-skill modules, map common import names to PyPI names.

Result schema mirrors the `admin_skills.dependencies` JSONB column:
    {
      "pip":  [{"name": "pandas", "version": ">=1.0", "source": "requirements.txt"}],
      "npm":  [...],
      "apt":  [...],
      "warnings": ["..."]
    }
"""
from __future__ import annotations

import ast
import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - backend runs on 3.11+
    tomllib = None  # type: ignore

__all__ = ["detect_dependencies", "IMPORT_TO_PYPI"]


# ── Common import-name → PyPI package-name map ───────────────────────────────
# Only well-known mismatches; for names not listed, the import name is used
# as-is (which is correct for the majority of packages).
IMPORT_TO_PYPI: Dict[str, str] = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "PyMuPDF",
    "dotenv": "python-dotenv",
    "magic": "python-magic",
    "Crypto": "pycryptodome",
    "OpenSSL": "pyOpenSSL",
    "serial": "pyserial",
    "MySQLdb": "mysqlclient",
    "pymysql": "PyMySQL",
    "psycopg2": "psycopg2-binary",
    "google.cloud": "google-cloud",
    "googleapiclient": "google-api-python-client",
    "win32com": "pywin32",
    "pythoncom": "pywin32",
    "jwt": "PyJWT",
    "attr": "attrs",
    "dateutil": "python-dateutil",
    "OpenGL": "PyOpenGL",
    "tlz": "toolz",
    "lxml": "lxml",
    "PySide2": "PySide2",
    "PyQt5": "PyQt5",
    "PyQt6": "PyQt6",
    "tomli": "tomli",
    "ruamel": "ruamel.yaml",
    "tensorflow_hub": "tensorflow-hub",
    "tensorflow_datasets": "tensorflow-datasets",
    "tensorflow_addons": "tensorflow-addons",
    "ujson": "ujson",
    "msgpack": "msgpack",
}


_REQUIREMENTS_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9._-]+)\s*(?P<spec>(?:[<>=!~]=?|==).+)?\s*(?:#.*)?$"
)


def _parse_requirements_txt(text: str) -> List[Dict[str, Any]]:
    """Parse a pip requirements.txt body. Best-effort, ignores -r / -e / urls."""
    out: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Drop env markers and extras for the name match
        # e.g. "package[extra]>=1.0 ; python_version>='3.8'"
        line_clean = line.split(";", 1)[0].strip()
        # strip extras
        line_clean = re.sub(r"\[.*?\]", "", line_clean)
        m = _REQUIREMENTS_LINE_RE.match(line_clean)
        if not m:
            continue
        name = m.group("name").strip()
        spec = (m.group("spec") or "").strip()
        if not name:
            continue
        out.append({"name": name, "version": spec or None})
    return out


def _parse_pyproject_toml(text: str) -> List[Dict[str, Any]]:
    """Parse PEP-621 [project.dependencies] and tool.poetry.dependencies."""
    if tomllib is None:
        return []
    try:
        data = tomllib.loads(text)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    proj_deps = (data.get("project") or {}).get("dependencies") or []
    if isinstance(proj_deps, list):
        for entry in proj_deps:
            if not isinstance(entry, str):
                continue
            parsed = _parse_requirements_txt(entry)
            out.extend(parsed)
    poetry_deps = (((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {})
    if isinstance(poetry_deps, dict):
        for name, value in poetry_deps.items():
            if name.lower() == "python":
                continue
            if isinstance(value, str):
                ver = value if value.strip() != "*" else None
                # poetry "^1.2" / "~1.2" → keep as-is, pip-compatible enough for display
                out.append({"name": name, "version": ver})
            elif isinstance(value, dict):
                ver = value.get("version")
                out.append({"name": name, "version": ver if ver and ver != "*" else None})
    return out


def _parse_package_json(text: str) -> List[Dict[str, Any]]:
    """Parse a package.json `dependencies` map. devDependencies are ignored."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    deps = data.get("dependencies") or {}
    if not isinstance(deps, dict):
        return []
    return [
        {"name": name, "version": str(ver) if ver and str(ver) != "*" else None}
        for name, ver in deps.items()
    ]


def _parse_apt_lines(text: str) -> List[Dict[str, Any]]:
    """One package per line, `#` comments allowed."""
    out: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        out.append({"name": line, "version": None})
    return out


# ── Static import scan ───────────────────────────────────────────────────────


_STDLIB = frozenset(sys.stdlib_module_names) | {
    # builtins that aren't in stdlib_module_names on some versions
    "__future__",
    "typing_extensions",  # bundled with most environments via stdlib backport guess
}


def _intra_skill_modules(extra_files: Dict[str, str]) -> set[str]:
    """Python module/package names defined inside the skill bundle.

    Skills routinely add a subdir (e.g. ``scripts/``) to ``sys.path`` and import
    their own subpackages by bare name (``from core.parser import ...`` where
    ``core`` is ``scripts/core/``). So we treat **every directory segment** and
    every ``.py`` file stem in the bundle as an intra-skill name — not just the
    top-level one — otherwise inner packages like ``core`` get mis-detected as
    PyPI dependencies. Over-filtering a bundled dir name is the safe direction:
    if the skill ships a ``core/`` dir, ``import core`` resolves to it, not PyPI.
    """
    mods: set[str] = set()
    for path in extra_files:
        if not path.endswith(".py"):
            continue
        parts = path.split("/")
        for seg in parts[:-1]:  # directory segments → intra-skill package names
            if seg:
                mods.add(seg)
        stem = parts[-1]
        if stem.endswith(".py"):
            mods.add(stem[:-3])
    return mods


def _is_test_file(path: str) -> bool:
    """Whether this is a test file (excluded from the runtime dependency scan).

    Test files only run in development/CI; their imports (pytest / hypothesis / internal modules under test) are not the skill's
    **runtime** dependencies, and scanning them in causes false positives (e.g. ``pytest``). Packages actually needed at runtime should be declared in requirements.txt.
    """
    segs = path.lower().split("/")
    base = segs[-1]
    if base == "conftest.py" or base.startswith("test_") or base.endswith("_test.py"):
        return True
    return any(seg in ("test", "tests") for seg in segs[:-1])


_DYNAMIC_IMPORT_RE = re.compile(r"\bimportlib\.import_module\b")


def _scan_py_imports(
    extra_files: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Return (deps, warnings). Each dep: {name, version=None, source='static_scan'}."""
    seen: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    intra = _intra_skill_modules(extra_files)

    for path, content in extra_files.items():
        if not path.endswith(".py") or _is_test_file(path):
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError as exc:
            warnings.append(f"{path}: 解析失败 ({exc.msg})")
            continue
        if _DYNAMIC_IMPORT_RE.search(content):
            warnings.append(f"{path}: 使用了 importlib.import_module，可能漏扫动态依赖")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    _register(top, seen, intra)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import, intra-skill
                if not node.module:
                    continue
                top = node.module.split(".", 1)[0]
                _register(top, seen, intra)

    return list(seen.values()), warnings


def _register(top: str, seen: Dict[str, Dict[str, Any]], intra: set[str]) -> None:
    if not top or top in _STDLIB or top in intra:
        return
    pypi = IMPORT_TO_PYPI.get(top, top)
    if pypi not in seen:
        seen[pypi] = {"name": pypi, "version": None, "source": "static_scan"}


# ── Top-level entry point ────────────────────────────────────────────────────


_SOURCE_PRIORITY = {
    "manual": 0,
    "requirements.txt": 1,
    "pyproject.toml": 2,
    "package.json": 2,
    "apt-requirements.txt": 2,
    "static_scan": 3,
}


def _merge(
    out: Dict[str, Dict[str, Any]],
    entry: Dict[str, Any],
    source: str,
) -> None:
    """Dedup by name; keep higher-priority source; prefer non-empty version."""
    name = entry["name"]
    existing = out.get(name)
    cand = {
        "name": name,
        "version": entry.get("version"),
        "source": source,
    }
    if existing is None:
        out[name] = cand
        return
    # Lower priority value wins.
    if _SOURCE_PRIORITY.get(source, 99) < _SOURCE_PRIORITY.get(existing["source"], 99):
        # New source is more authoritative
        if not cand["version"] and existing.get("version"):
            cand["version"] = existing["version"]
        out[name] = cand
    else:
        # Keep existing source but fill in version if missing.
        if not existing.get("version") and cand.get("version"):
            existing["version"] = cand["version"]


def detect_dependencies(extra_files: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Analyze extra_files dict (filename → text content) and return a dependencies manifest."""
    if not extra_files:
        return {"pip": [], "npm": [], "apt": [], "warnings": []}

    pip: Dict[str, Dict[str, Any]] = {}
    npm: Dict[str, Dict[str, Any]] = {}
    apt: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []

    for path, content in extra_files.items():
        base = path.rsplit("/", 1)[-1].lower()
        try:
            if base.startswith("requirements") and base.endswith(".txt"):
                for entry in _parse_requirements_txt(content):
                    _merge(pip, entry, "requirements.txt")
            elif base == "pyproject.toml":
                for entry in _parse_pyproject_toml(content):
                    _merge(pip, entry, "pyproject.toml")
            elif base == "package.json":
                for entry in _parse_package_json(content):
                    _merge(npm, entry, "package.json")
            elif base in ("apt-requirements.txt", "aptfile", "apt.txt"):
                for entry in _parse_apt_lines(content):
                    _merge(apt, entry, "apt-requirements.txt")
        except Exception as exc:  # defensive: never crash detector on a bad file
            warnings.append(f"{path}: 解析失败 ({exc})")

    static_deps, static_warnings = _scan_py_imports(extra_files)
    for entry in static_deps:
        _merge(pip, entry, "static_scan")
    warnings.extend(static_warnings)

    return {
        "pip": sorted(pip.values(), key=lambda d: d["name"].lower()),
        "npm": sorted(npm.values(), key=lambda d: d["name"].lower()),
        "apt": sorted(apt.values(), key=lambda d: d["name"].lower()),
        "warnings": warnings,
    }
