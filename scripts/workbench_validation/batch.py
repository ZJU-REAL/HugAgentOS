"""Serial WB-23 runner. Commands need a reviewed manifest and a frozen source tree."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
from urllib.parse import urlparse
import signal
import subprocess
import sys
import time

from scripts.workbench_validation.matrix import BASELINE, inventory, required_cases
from scripts.workbench_validation.report import merge_results, write_report

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = ROOT / ".scratch/workbench-23"


def fingerprint(root):
    paths = subprocess.check_output(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
    digest = hashlib.sha256()
    for name in sorted(set(paths)):
        if not name or name.startswith((".scratch/", ".env", "internal design docs")):
            continue
        path = root / name
        if path.is_file():
            digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def machine():
    cpu = "unknown"
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    return dict(system=platform.platform(), architecture=platform.machine(), cpu=cpu,
                logicalCpus=os.cpu_count(),
                memoryBytes=os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
                python=platform.python_version(),
                buildMode="not-recorded", browserVersion="not-recorded")


def output_path(name):
    path = Path(name).resolve()
    if not path.is_relative_to(SCRATCH.resolve()):
        raise ValueError("Output must be under this checkout's .scratch/workbench-23")
    path.mkdir(parents=True, exist_ok=True)
    return path


def redact(text, env):
    for key, value in env.items():
        if re.search(r"token|password|secret|api_key", key, re.I) and len(str(value)) >= 3:
            text = text.replace(str(value), "[REDACTED]")
    text = re.sub(r"(?i)Bearer\s+[\w.+=/-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"://[^/@\s]+:[^/@\s]+@", "://[REDACTED]@", text)
    return text


def run_command(command, output, number):
    if command.get("reviewed") is not True:
        return dict(id=command["id"], status="blocked", reason="Command source/targets not reviewed")
    cwd = (ROOT / command["cwd"]).resolve()
    if not cwd.is_relative_to(ROOT) or not cwd.is_dir():
        raise ValueError("Command cwd must be inside the checkout")
    argv = command["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("Command argv must be a nonempty string array")
    env = dict(os.environ)
    temp = output / f"command-{number}"
    temp.mkdir(exist_ok=True)
    env.update(WB23_CASES_PATH=str(output / "required-cases.json"), WB23_SOURCE_FINGERPRINT=fingerprint(ROOT), TMPDIR=str(temp), TMP=str(temp), TEMP=str(temp),
               STORAGE_PATH=str(temp / "storage"),
               DATABASE_URL="sqlite:///" + str(temp / "test.db"),
               PYTHONPATH=str(ROOT / "src/backend"), PYTHONDONTWRITEBYTECODE="1")
    # Explicit reviewed environments can select a disposable PostgreSQL DB.
    # Do not serialize their values or argv, which can contain private test credentials.
    for key, value in command.get("env", {}).items():
        if key in ("STORAGE_PATH", "TMPDIR", "TMP", "TEMP"):
            if not Path(value).resolve().is_relative_to(SCRATCH.resolve()):
                raise ValueError("Test data paths must stay in WB-23 scratch")
        if key == "DATABASE_URL":
            parsed = urlparse(value)
            sqlite = parsed.scheme in ("sqlite", "sqlite+aiosqlite")
            sqlite_path = Path(parsed.path.removeprefix("/")).resolve() if sqlite else None
            local_pg = (parsed.scheme in ("postgresql", "postgresql+psycopg", "postgresql+psycopg2")
                        and parsed.hostname in ("localhost", "127.0.0.1", "::1")
                        and re.fullmatch(r"/workbench_23_[a-z0-9_]+", parsed.path)
                        and not parsed.query and not parsed.fragment)
            if not ((sqlite_path and not parsed.netloc and not parsed.query and not parsed.fragment
                     and sqlite_path.is_relative_to(SCRATCH.resolve())) or local_pg):
                raise ValueError("Database must be an isolated WB-23 database")
        env[key] = value
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    timed_out = False
    try:
        stdout, _ = process.communicate(timeout=command.get("timeoutSeconds", 900))
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, _ = process.communicate()
    result = dict(id=command["id"], sourceFingerprint=fingerprint(ROOT), level=command["level"],
                  status="fail" if timed_out or process.returncode else "pass",
                  returncode=process.returncode, timeout=timed_out,
                  elapsedSeconds=round(time.monotonic() - started, 3),
                  outputSha256=hashlib.sha256(stdout).hexdigest(),
                  outputBytes=len(stdout), publicCommand=redact(command.get("publicCommand", Path(argv[0]).name + " [arguments omitted]"), env),
                  cwd=command["cwd"])
    if command.get("captureSyntheticOutput") is True:
        log = output / f"command-{number}.log"
        log.write_text(redact(stdout.decode(errors="replace"), env))
        result["outputEvidence"] = dict(path=str(log.relative_to(ROOT)),
                                        sha256=hashlib.sha256(log.read_bytes()).hexdigest())
    # Machine-readable ESLint reports preserve exact counts without printing source.
    if command.get("eslintJson"):
        try:
            entries = json.loads(stdout)
            result["lint"] = dict(errors=sum(e["errorCount"] for e in entries),
                                 warnings=sum(e["warningCount"] for e in entries),
                                 files=[dict(path=str(Path(e["filePath"]).relative_to(ROOT)),
                                             errors=e["errorCount"], warnings=e["warningCount"])
                                        for e in entries if e["errorCount"] or e["warningCount"]])
        except (ValueError, KeyError, TypeError):
            result["status"] = "fail"
            result["reason"] = "ESLint JSON report was invalid"
    (output / f"command-{number}.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "freeze", "run", "report"))
    parser.add_argument("--output", default=str(SCRATCH / "prepared"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--freeze", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--command-results", type=Path)
    args = parser.parse_args()
    output = output_path(args.output)
    current = fingerprint(ROOT)
    if args.mode == "freeze":
        (output / "freeze.json").write_text(json.dumps(dict(
            sourceFingerprint=current, head=subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()), indent=2))
        print("Frozen source:", current)
        return 0
    cases = required_cases(ROOT)
    (output / "required-cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2))
    commands = []
    if args.mode == "plan":
        found = inventory(ROOT)
        (output / "inventory.json").write_text(json.dumps(found, ensure_ascii=False, indent=2))
        commands = [dict(id=c["name"], argv=c["argv"], cwd=c["cwd"],
                         level="static" if c["name"] in ("lint", "build") else "component",
                         reviewed=False, status="not-run") for c in found["frontend"]]
        (output / "commands.json").write_text(json.dumps(dict(commands=commands), indent=2))
    else:
        if not args.freeze or json.loads(args.freeze.read_text())["sourceFingerprint"] != current:
            raise ValueError("Missing freeze or source changed since freeze")
        if args.mode == "report" and args.command_results:
            previous = json.loads(args.command_results.read_text())
            if previous["sourceFingerprint"] != current or not previous["sourceUnchanged"]:
                raise ValueError("Stale command report")
            commands = previous["commands"]
        if args.mode == "run":
            if not args.manifest:
                raise ValueError("A reviewed command manifest is required")
            commands = [run_command(command, output, i) for i, command in enumerate(
                json.loads(args.manifest.read_text())["commands"])]
    if args.results:
        cases = merge_results(ROOT, cases, json.loads(args.results.read_text()), current)
    unchanged = fingerprint(ROOT) == current
    report = dict(baseline=BASELINE, sourceFingerprint=current, sourceUnchanged=unchanged,
                  machine=machine(), commands=commands, cases=cases,
                  webComplete=unchanged and bool(commands) and
                  all(c["status"] == "pass" for c in commands) and
                  all(c["status"] == "pass" for c in cases if c["level"] != "native"),
                  complete=unchanged and bool(commands) and
                  all(c["status"] == "pass" for c in commands) and
                  all(c["status"] == "pass" for c in cases))
    write_report(output, report)
    print(f"Report: {output / 'report.json'}; complete={report['complete']}")
    return 0 if args.mode == "plan" or report["complete"] else 1


if __name__ == "__main__":
    try:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with (SCRATCH / "batch.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Another WB-23 batch is running")
            raise SystemExit(main())
    except (ValueError, KeyError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
