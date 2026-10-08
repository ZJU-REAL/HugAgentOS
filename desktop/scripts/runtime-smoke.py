#!/usr/bin/env python3
"""Smoke-test the relocatable Python runtime embedded in desktop bundles."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

NATIVE_PROCESS_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

REQUIRED_MODULES = (
    "agentscope",
    "fastapi",
    "httpx",
    "mcp",
    "numpy",
    "pandas",
    "playwright",
    "pikepdf",
    "pymilvus",
    "scipy",
    "sqlalchemy",
    "uvicorn",
    "watchdog.observers",
)


def check_bash_runtime(root: Path, tools: dict) -> dict:
    """Run real file tools using only the shipped Windows shell and DLLs."""
    tool = tools.get("git-bash")
    if not isinstance(tool, dict) or tool.get("path") != "native/git-bash/usr/bin/bash.exe":
        raise RuntimeError("Bundled Bash is missing from native-tools.json")
    binary = (root / tool["path"]).resolve()
    binary.relative_to(root.resolve())
    if not binary.is_file():
        raise RuntimeError("Bundled Bash executable is missing")
    from runtime_tools import windows_tool_path_entries
    env = {key: value for key, value in os.environ.items()
           if key in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR")}
    # Use the runner's real GNU-before-System32 order, never inherited host Git.
    env["PATH"] = os.pathsep.join(windows_tool_path_entries(str(binary)))
    script = r"""
for tool in bash sh find sort head cut grep sed awk cat mkdir rm cp mv ls wc xargs tr; do
    command -v "$tool" >/dev/null
done
mkdir -p '中文 文件'
printf 'needle\nother\n' > '中文 文件/input.txt'
cp '中文 文件/input.txt' '中文 文件/copy.txt'
mv '中文 文件/copy.txt' '中文 文件/moved.txt'
test "$(find . -type f -name '*.txt' -printf '%T@ %p\n' | sort -rn | head -1 | cut -d' ' -f2-)" != ''
test "$(grep -rE --include='*.txt' -l needle . | wc -l | tr -d ' ')" = 2
test "$(cat '中文 文件/input.txt' | sed -n '1p' | awk '{print $1}')" = needle
test "$(printf 'ok' | xargs printf '%s')" = ok
ls '中文 文件' >/dev/null
rm '中文 文件/moved.txt'
test ! -e '中文 文件/moved.txt'
printf 'HUGAGENT_PRIVATE_BASH_OK\n'
"""
    with tempfile.TemporaryDirectory(prefix="bash smoke 中文 ") as folder:
        env["HOME"] = folder
        result = subprocess.run(
            [str(binary), "--noprofile", "--norc", "-e", "-u", "-o", "pipefail", "-c", script],
            cwd=folder, env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=60, **NATIVE_PROCESS_FLAGS)
        if result.returncode != 0:
            raise RuntimeError("Bundled Bash file-tool smoke test failed: " + result.stderr[-2000:])
        if result.stdout.strip() != "HUGAGENT_PRIVATE_BASH_OK":
            raise RuntimeError("Bundled Bash file-tool smoke test failed")
    return {"git-bash": tool["version"]}


def check_native_tools() -> dict:
    root = Path(__file__).resolve().parent
    tools = json.loads((root / "native-tools.json").read_text(encoding="utf-8"))
    bash_versions = check_bash_runtime(root, tools) if os.name == "nt" else {}
    binaries = {}
    env = {key: value for key, value in os.environ.items()
           if key in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "HOME", "USERPROFILE", "LANG")}
    env["DOTNET_EnableDiagnostics"] = "0"
    env["OFFICECLI_SKIP_UPDATE"] = "1"
    if sys.platform.startswith("linux"):
        env["SAL_USE_VCLPLUGIN"] = "svp"
    for name in ("officecli", "pandoc", "libreoffice"):
        if name not in tools:
            raise RuntimeError(f"Bundled {name} is missing from native-tools.json")
        tool = tools[name]
        binary = root / tool["path"]
        binary.resolve().relative_to(root)
        if not binary.is_file():
            raise RuntimeError(f"Bundled {name} is missing")
        binaries[name] = binary
        env["PATH"] = str(binary.parent) + os.pathsep + os.defpath
        result = subprocess.run([str(binary), "--version"], env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=60, check=True, **NATIVE_PROCESS_FLAGS)
        ending = r"(?![0-9])" if name == "libreoffice" else r"(?![0-9.])"
        if not re.search(r"(?<![0-9.])" + re.escape(tool["version"]) + ending,
                         result.stdout + result.stderr):
            raise RuntimeError(f"Bundled {name} version does not match the manifest")
    # Exercise conversion after relocation, not merely --version or host PATH.
    with tempfile.TemporaryDirectory(prefix="office-smoke-") as folder:
        work = Path(folder)
        markdown = work / "source.md"
        markdown.write_text("# Desktop offline test\n\nBundledOfficeRoundtrip\n", encoding="utf-8")
        docx = work / "source.docx"
        subprocess.run([str(binaries["pandoc"]), str(markdown), "-o", str(docx)],
                       env=env, capture_output=True, timeout=60, check=True, **NATIVE_PROCESS_FLAGS)
        parsed = subprocess.run([str(binaries["pandoc"]), str(docx), "-t", "plain"],
                                env=env, capture_output=True, text=True, timeout=60, check=True, **NATIVE_PROCESS_FLAGS)
        if "BundledOfficeRoundtrip" not in parsed.stdout:
            raise RuntimeError("Bundled Pandoc DOCX roundtrip failed")
        subprocess.run([str(binaries["libreoffice"]),
                        "-env:UserInstallation=" + (work / "profile").as_uri(),
                        "--headless", "--convert-to", "pdf", "--outdir", str(work), str(docx)],
                       env=env, capture_output=True, timeout=120, check=True, **NATIVE_PROCESS_FLAGS)
        pdf = work / "source.pdf"
        if not pdf.is_file() or not pdf.read_bytes().startswith(b"%PDF-"):
            raise RuntimeError("Bundled LibreOffice DOCX-to-PDF conversion failed")
    return {**bash_versions, **{name: tools[name]["version"] for name in binaries}}




def check_file_watcher() -> None:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    changed = threading.Event()

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if Path(event.src_path).name == "watchdog-probe.txt":
                changed.set()

    with tempfile.TemporaryDirectory(prefix="desktop-watcher-") as folder:
        observer = Observer()
        observer.schedule(Handler(), folder)
        observer.start()
        try:
            (Path(folder) / "watchdog-probe.txt").write_text("probe", encoding="utf-8")
            if not changed.wait(10):
                raise RuntimeError("Bundled file watcher did not receive a file creation event")
        finally:
            observer.stop()
            observer.join(timeout=10)
        if observer.is_alive():
            raise RuntimeError("Bundled file watcher failed to stop")


def check_application(source: Path) -> None:
    """Load all application routers without touching the builder's data or env."""
    source = source.resolve()
    backend = source / "src" / "backend"
    if not (backend / "cli.py").is_file():
        raise RuntimeError(f"Desktop source is missing cli.py: {backend}")
    with tempfile.TemporaryDirectory(prefix="desktop-app-smoke-") as folder:
        env = {key: value for key, value in os.environ.items()
               if key.upper() in ("SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "PATH", "LANG")}
        env.update(HOME=folder, USERPROFILE=folder,
                   HUGAGENT_HOME=str(Path(folder) / "data"),
                   HUGAGENT_CAPS_ROOT=str(Path(folder) / "caps"),
                   PYTHONPATH=str(backend), PYTHONDONTWRITEBYTECODE="1",
                   HUGAGENT_BOOTSTRAP_DEFAULT_PLUGINS="0")
        result = subprocess.run(
            [sys.executable, "-c",
             "import cli; cli.apply_local_env(33291); from api.app import app; "
             "print('DESKTOP_APPLICATION_IMPORT_OK')"],
            cwd=source, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90, **NATIVE_PROCESS_FLAGS)
        if result.returncode or "DESKTOP_APPLICATION_IMPORT_OK" not in result.stdout.splitlines():
            raise RuntimeError("Desktop application import failed:\n" + result.stderr[-6000:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--bash-only", action="store_true")
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    if args.source_only:
        if not args.source:
            parser.error("--source-only requires --source")
        check_application(args.source)
        print(json.dumps({"ok": True, "modules": ["cli", "api.app"]}))
        return 0

    if args.bash_only:
        root = Path(__file__).resolve().parent
        tools = json.loads((root / "native-tools.json").read_text(encoding="utf-8"))
        print(json.dumps({"ok": True, "native_tools": check_bash_runtime(root, tools)}))
        return 0

    native_tools = check_native_tools()
    browser_script = Path(__file__).resolve().parent / "browser-runtime.py"
    subprocess.run([sys.executable, str(browser_script), "--runtime", str(browser_script.parent), "--check"], check=True, timeout=60, **NATIVE_PROCESS_FLAGS)
    if args.native_only:
        print(json.dumps({"ok": True, "native_tools": native_tools}))
        return 0

    imported: list[str] = []
    for module_name in REQUIRED_MODULES:
        importlib.import_module(module_name)
        imported.append(module_name)

    check_file_watcher()
    if args.source:
        check_application(args.source)
        imported.extend(["cli", "api.app"])

    print(
        json.dumps(
            {
                "ok": True,
                "python": platform.python_version(),
                "executable": sys.executable,
                "modules": imported,
                "native_tools": native_tools,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
