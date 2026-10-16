"""Strict report ingestion: an exit code alone cannot satisfy a behavior case."""
import hashlib
import json
from pathlib import Path

LEVELS = {"component", "fixture-browser", "real-http", "real-browser", "native", "static"}
STATUSES = {"pass", "fail", "blocked", "not-run"}


def evidence(root, paths):
    result = []
    for name in paths:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f"Missing or out-of-scope evidence: {name}")
        result.append(dict(path=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return result


def merge_results(root, cases, records, fingerprint):
    indexed = {case["id"]: dict(case) for case in cases}
    seen = set()
    for record in records:
        ident = record["id"]
        if ident not in indexed or ident in seen:
            raise ValueError(f"Unknown or duplicate case: {ident}")
        seen.add(ident)
        status = record["status"]
        if status not in STATUSES or record["level"] not in LEVELS:
            raise ValueError(f"Invalid result classification: {ident}")
        if record.get("sourceFingerprint") != fingerprint:
            raise ValueError(f"Stale source evidence: {ident}")
        if status == "pass":
            if record["level"] != indexed[ident]["level"]:
                raise ValueError(f"Evidence level cannot satisfy this case: {ident}")
            if not record.get("actual") or not record.get("steps") or not record.get("evidence"):
                raise ValueError(f"Pass requires actual, steps and evidence: {ident}")
        if status == "pass" and ident.startswith("UI-"):
            observed = record.get("dimensions", {})
            for field in ("layout", "applications", "height", "theme", "language"):
                if observed.get(field) != indexed[ident][field]:
                    raise ValueError(f"Unverified matrix dimension: {ident}/{field}")
        if status == "pass" and ident in ("A15", "PERF-100"):
            performance = record.get("performance", {})
            if (performance.get("sampleCount", 0) < 100 or
                    performance.get("p95Ms", float("inf")) > 100 or
                    performance.get("applicationCount", 0) < 100 or
                    performance.get("unopenedIframeCount") != 0 or
                    performance.get("unopenedProcessCount") != 0 or
                    performance.get("runtimeComplete") is not True or
                    performance.get("runtimeProvider") != "script_runner" or
                    performance.get("runtimeObservation") != "actual_local_process_handles" or
                    performance.get("startingWorkerCount") != 0 or
                    performance.get("unknownWorkerCount") != 0 or
                    not all(performance.get(key) for key in
                            ("cpu", "memoryBytes", "browserVersion", "buildMode"))):
                raise ValueError(f"Incomplete or failing performance evidence: {ident}")
            if not any("performance" in name for name in record.get("evidence", [])):
                raise ValueError(f"Performance file not attached: {ident}")
        checked = evidence(root, record.get("evidence", []))
        indexed[ident].update(record, evidence=checked)
    return list(indexed.values())


def write_report(output, report):
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    counts = {state: sum(case["status"] == state for case in report["cases"])
              for state in STATUSES}
    lines = ["# WB-23 batch report", "",
             f"Source fingerprint: {report['sourceFingerprint']}",
             f"Baseline: {report['baseline']}",
             "Status: complete" if report["complete"] else "Status: incomplete",
             f"Counts: {json.dumps(counts, sort_keys=True)}", "",
             "Command results are separate from behavioral acceptance.", "",
             "| Case | Level | Status | Expected |", "|---|---|---|---|"]
    for case in report["cases"]:
        expected = case["expected"].replace("|", "/").replace("\n", " ")
        lines.append(f"| {case['id']} | {case['level']} | {case['status']} | {expected} |")
    (output / "report.md").write_text("\n".join(lines) + "\n")
