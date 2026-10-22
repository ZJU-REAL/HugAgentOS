"""Native Windows acceptance check for the private Bash archive.

Run from the Windows build checkout:
  python desktop/scripts/verify-windows-bash.py --archive <pinned-git.tar.bz2> --output <new-temp-dir>
The isolated output is retained for inspection; no installed application is changed.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Run this acceptance check with native Windows Python")
    scripts = Path(__file__).resolve().parent
    root = args.output.resolve()
    # Never reuse an activated runtime or delete an existing directory.
    root.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((scripts.parent / "native-tools.json").read_text(encoding="utf-8"))
    tool = manifest["git-bash"]
    tool["targets"]["windows-x86_64"]["url"] = args.archive.resolve(strict=True).as_uri()
    source = root / "tools.json"
    source.write_text(json.dumps({"schema": 1, "git-bash": tool}), encoding="utf-8")
    runtime = root / "original"
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW}
    def run(*arguments, **kwargs):
        return subprocess.run([sys.executable, "-X", "utf8", *map(str, arguments)], check=True,
                              capture_output=True, text=True, encoding="utf-8", **flags, **kwargs)
    run(scripts / "install-native-tools.py", "--manifest", source,
        "--target", "windows-x86_64", "--runtime", runtime, "--executable", "python/python.exe")
    shutil.copy2(scripts / "runtime-smoke.py", runtime / "runtime-smoke.py")
    shutil.copy2(scripts.parents[1] / "src/backend/core/sandbox/runner/runtime_tools.py",
                 runtime / "runtime_tools.py")
    (runtime / "runtime-layout.json").write_text('{"schema":1}', encoding="utf-8")
    archive = root / "runtime-core.tar.gz"
    run(scripts / "create-runtime-archive.py", "--source", runtime, "--output", archive)
    relocated = root / "relocated 中文 runtime"
    with tarfile.open(archive) as bundle:
        bundle.extractall(relocated, filter="data")
    env = {key: value for key, value in os.environ.items()
           if key in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP")}
    env["PATH"] = ""
    smoke = relocated / "runtime-smoke.py"
    resolver = (
        "import json, sys; from pathlib import Path; "
        "sys.executable = str(Path.cwd() / 'python/python.exe'); "
        "from runtime_tools import resolve_bash_executable; "
        "print(json.dumps(resolve_bash_executable()))"
    )
    resolved = json.loads(run("-c", resolver, env=env, cwd=relocated).stdout)
    assert Path(resolved) == relocated / "native/git-bash/usr/bin/bash.exe"
    result = run(smoke, "--bash-only", env=env)
    assert json.loads(result.stdout)["ok"]
    # Missing dependencies must stop activation, even if host Git is installed.
    grep = relocated / "native/git-bash/usr/bin/grep.exe"
    saved = grep.with_suffix(".disabled")
    grep.rename(saved)
    try:
        result = subprocess.run([sys.executable, "-X", "utf8", str(smoke), "--bash-only"], env=env,
                                capture_output=True, text=True, encoding="utf-8", **flags)
        assert result.returncode != 0, "Missing grep unexpectedly passed"
    finally:
        saved.rename(grep)
    run(smoke, "--bash-only", env=env)
    print(json.dumps({"ok": True, "checks": ["verified-native-staging", "archive-relocation",
          "private-runner-resolution", "GNU-before-System32-Chinese-file-tools", "missing-command-rejected"],
          "runtime": str(relocated), "archive_bytes": archive.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
