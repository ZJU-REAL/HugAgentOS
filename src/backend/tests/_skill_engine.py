"""Load a document skill's vendored ``engine`` package under a unique alias.

Each of the word / excel / pdf editing skills ships its own
``scripts/engine/`` package. They all share the bare name ``engine``;
importing two of them in one pytest process under that name would collide
in ``sys.modules`` and silently hand back the wrong skill's modules.

This helper registers each engine under a distinct top-level alias
(``word_engine`` / ``xlsx_engine`` / ``pdf_engine``) so they coexist. The
engines use only relative imports internally, so once the package is
registered its submodules import cleanly as ``<alias>.<submodule>``.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

# Since eafa78f, built-in document skills are layered under skill_bundles/default/ (installable
# marketplace skills live in marketplace/); the three word/excel/pdf editing engines all belong to the built-in layer
_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skill_bundles" / "default"


def load_engine(skill_dir: str, alias: str) -> ModuleType:
    """Import ``<skill_dir>/scripts/engine`` as a top-level module named ``alias``.

    Idempotent — a repeat call with the same alias returns the cached module.
    After loading, submodules are importable via ``engine_submodule`` or a
    plain ``importlib.import_module(f"{alias}.<name>")``.
    """
    if alias in sys.modules:
        return sys.modules[alias]
    engine_dir = _SKILLS_ROOT / skill_dir / "scripts" / "engine"
    init_file = engine_dir / "__init__.py"
    if not init_file.is_file():
        raise FileNotFoundError(f"engine package not found: {init_file}")
    spec = importlib.util.spec_from_file_location(
        alias, init_file, submodule_search_locations=[str(engine_dir)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def engine_submodule(alias: str, name: str) -> ModuleType:
    """Import and return ``<alias>.<name>`` (engine must be loaded first)."""
    return importlib.import_module(f"{alias}.{name}")
