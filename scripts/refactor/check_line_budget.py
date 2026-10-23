"""Reject new oversized Python modules and growth of existing exceptions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "internal design docs"


def violations(root: Path, baseline: dict[str, int]) -> list[str]:
    errors = []
    for directory in ("src/backend", "ce/overlay/src/backend", "scripts/refactor"):
        for path in sorted((root / directory).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            count = len(path.read_text(encoding="utf-8-sig").splitlines())
            limit = max(500, baseline.get(relative, 500))
            if count > limit:
                errors.append(f"{relative}: {count} lines exceeds {limit}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args()
    errors = violations(ROOT, json.loads(args.baseline.read_text()))
    for error in errors:
        print(error)
    if not errors:
        print("Line budget: passed (existing exceptions may only shrink)")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
