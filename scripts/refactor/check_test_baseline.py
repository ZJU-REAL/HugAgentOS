"""Compare a completed pytest run against the original failure set."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


def result(log: str) -> dict:
    summaries = re.findall(r"^=+ (.*\bin [0-9.]+s(?: \([^\n]*\))?) =+$", log, re.MULTILINE)
    if not summaries or not re.search(r"\d+ (?:passed|failed|error|skipped)", summaries[-1]):
        raise ValueError("pytest has not produced a complete final summary")
    if "Interrupted:" in log or "INTERNALERROR>" in log:
        raise ValueError("interrupted or internally failed pytest run is not a baseline")
    failures = []
    # Application logs can also start with ERROR; only parse pytest's final list.
    failure_section = log.rsplit("short test summary info", 1)[-1]
    for line in failure_section.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            failures.append(line.split(" ", 1)[1].split(" - ", 1)[0])
    reported = re.search(r"(\d+) failed", summaries[-1])
    errors = re.search(r"(\d+) errors?", summaries[-1])
    expected = sum(int(match[1]) for match in (reported, errors) if match)
    if len(failures) != expected:
        raise ValueError("failure list is incomplete; keep the full pytest report")
    return {"summary": summaries[-1], "failures": sorted(set(failures))}


def canonical_failures(failures: list[str], moves: dict[str, str]) -> set[str]:
    """Compare original node IDs after explicit test-file moves, keeping parameters intact."""
    destinations = set()
    for old, new in moves.items():
        for path in (old, new):
            if not isinstance(path, str) or not path.startswith("src/backend/tests/") or not path.endswith(".py") or ".." in Path(path).parts or "\\" in path:
                raise ValueError(f"Invalid test migration path: {path!r}")
        if old == new or new in destinations or new in moves:
            raise ValueError(f"Ambiguous test migration: {old} -> {new}")
        destinations.add(new)
    reverse = {new: old for old, new in moves.items()}
    normalized = set()
    for failure in failures:
        path, separator, test = failure.partition("::")
        normalized.add(reverse.get(path, path) + separator + test)
    return normalized


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--save", type=Path)
    mode.add_argument("--baseline", type=Path)
    parser.add_argument("--test-moves", type=Path, help="Reviewed original-to-current test file mapping")
    args = parser.parse_args()
    if args.save and args.test_moves:
        parser.error("save the original baseline without migration normalization")
    actual = result(args.log.read_text())
    if args.save:
        if args.save.exists():
            parser.error("baseline already exists; preserve the original evidence")
        args.save.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n")
    else:
        expected = json.loads(args.baseline.read_text())
        moves = json.loads(args.test_moves.read_text()) if args.test_moves else {}
        introduced = sorted(canonical_failures(actual["failures"], moves) - set(expected["failures"]))
        if introduced:
            print("New test failures:\n" + "\n".join(introduced))
            return 1
    print(actual["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
