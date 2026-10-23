"""Validate source paths in the CE derivation manifest."""
from __future__ import annotations

import ast
import fnmatch
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def path_errors(root: Path, manifest: dict, tracked: list[str]) -> list[str]:
    errors = []
    optional = set(manifest.get("optional_safety_excludes", []))
    excludes = manifest.get("exclude", [])
    for pattern in optional - set(excludes):
        errors.append(f"Safety exception has no exclusion: {pattern}")
    for pattern in excludes:
        if pattern not in optional and not any(fnmatch.fnmatch(p, pattern) and (root / p).is_file() for p in tracked):
            errors.append(f"exclude: no source file matches {pattern}")
    overlay = root / manifest["overlay"]
    for path in manifest.get("split", []):
        if not (overlay / path).is_file():
            errors.append(f"split: missing overlay {path}")
    # Prunes run before overlay. Check only declared input paths, not output
    # names, replacement text, dictionary values, or forbidden module names.
    for name, rule in manifest.get("prunes", {}).items():
        inputs = []
        if "path" in rule:
            inputs.append(rule["path"])
        inputs.extend(rule.get("paths", []))
        inputs.extend(rule.get("dockerfiles", []))
        inputs.extend(rule.get("text_paths", []))
        inputs.extend(item["path"] for item in rule.get("files", []))
        for path in inputs:
            if not (root / path).exists():
                errors.append(f"prunes.{name}: missing source {path}")
    generated = {
        rule["optional_out"] for rule in manifest.get("prunes", {}).values()
        if "optional_out" in rule
    }
    generated.update(pair[1] for pair in manifest.get("renames", []))
    for path in manifest.get("required_files", []):
        if path not in generated and not (root / path).is_file() and not (overlay / path).is_file():
            errors.append(f"required_files: no source or overlay {path}")
    for source, _target in manifest.get("renames", []):
        if not (root / source).exists():
            errors.append(f"renames: missing source {source}")
    # CE overlays must not restore old aggregate exports after ownership moves.
    for relative in ("src/backend/core/db/models/__init__.py", "src/backend/core/db/repository/__init__.py"):
        path = overlay / relative
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            modules = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                       else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            for module in modules:
                if module.split(".")[0] in {"features", "integrations", "personal_agent"}:
                    errors.append(f"overlay aggregate forwards moved ownership: {relative} -> {module}")
    return sorted(errors)


def main():
    manifest = yaml.safe_load((ROOT / "ce/manifest.yaml").read_text())
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT, text=True
    ).split("\0")
    errors = path_errors(ROOT, manifest, tracked)
    for error in errors:
        print(error)
    if not errors:
        print("CE manifest paths: passed")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
