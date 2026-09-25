#!/usr/bin/env python3
"""Stage checksum-pinned native tools on release builders, without system installation."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


def safe_relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
        raise ValueError("Native tool path must stay inside the runtime")
    return path


def download(asset, destination):
    for attempt in range(3):
        try:
            digest, size = hashlib.sha256(), 0
            req = urllib.request.Request(asset["url"], headers={"User-Agent": "desktop-runtime-builder"})
            with urllib.request.urlopen(req, timeout=120) as response, destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > asset["size"]:
                        raise ValueError("Native tool download exceeds pinned size")
                    digest.update(chunk)
                    output.write(chunk)
            break
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    if size != asset["size"] or digest.hexdigest() != asset["sha256"]:
        raise ValueError("Native tool SHA-256/size verification failed")


def unpack(archive, destination, kind):
    """Extract files and confined links without letting paths escape staging."""
    destination.mkdir(parents=True, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(archive) as bundle:
            links = []
            for entry in bundle.infolist():
                relative = safe_relative(entry.filename)
                target = destination.joinpath(*relative.parts)
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    links.append((target, bundle.read(entry).decode("utf-8")))
                    continue
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(entry) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    target.chmod((entry.external_attr >> 16) & 0o777 or 0o644)
            for target, value in links:
                link = safe_relative(value)
                source = target.parent.joinpath(*link.parts)
                if not source.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Native archive link escapes staging")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(value)
    else:
        with tarfile.open(archive) as bundle:
            links = []
            for entry in bundle:
                relative = safe_relative(entry.name)
                target = destination.joinpath(*relative.parts)
                if entry.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif entry.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(entry) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out)
                    target.chmod(entry.mode & 0o777)
                elif entry.issym() or entry.islnk():
                    links.append((entry, target))
                else:
                    raise ValueError("Native archive contains a special file")
            # Create links last, so archive writes cannot follow them outside staging.
            for entry, target in links:
                link = safe_relative(entry.linkname)
                source = (target.parent if entry.issym() else destination).joinpath(*link.parts)
                if not source.resolve().is_relative_to(destination.resolve()):
                    raise ValueError("Native archive link escapes staging")
                target.parent.mkdir(parents=True, exist_ok=True)
                if entry.issym():
                    target.symlink_to(entry.linkname)
                else:
                    os.link(source, target)


def single(matches, label):
    matches = list(matches)
    if len(matches) != 1:
        raise ValueError(f"Expected one {label}; found {len(matches)}")
    return matches[0]


def stage_libreoffice(archive, asset, destination, target, scratch):
    kind = asset["format"]
    if kind == "dmg":
        if sys.platform != "darwin":
            raise ValueError("LibreOffice DMG requires a native macOS builder")
        mount = scratch / "mount"
        mount.mkdir()
        subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount),
                        str(archive)], check=True, capture_output=True)
        try:
            app = single(mount.glob("LibreOffice.app"), "LibreOffice app")
            subprocess.run(["ditto", str(app), str(destination / "LibreOffice.app")], check=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mount)], check=True, capture_output=True)
        return destination / "LibreOffice.app/Contents/MacOS/soffice"
    if kind == "msi":
        if os.name != "nt":
            raise ValueError("LibreOffice MSI requires a native Windows builder")
        # MSI administrative extraction uses legacy MAX_PATH handling. Keep its
        # staging path short even when the release checkout is deeply nested.
        with tempfile.TemporaryDirectory(prefix="lo-admin-") as folder:
            staging = Path(folder)
            msi = staging / "LibreOffice.msi"
            shutil.copy2(archive, msi)
            extracted = staging / "image"
            # Administrative extraction only: no registration or reboot.
            result = subprocess.run(["msiexec.exe", "/a", str(msi), "/qn", "/norestart",
                                     "TARGETDIR=" + str(extracted)], timeout=600)
            if result.returncode != 0:
                raise RuntimeError(f"LibreOffice administrative extraction failed: {result.returncode}")
            binary = single(extracted.rglob("program/soffice.exe"), "LibreOffice program")
            # Rename the extracted tree as a unit. Copying individual files into
            # the deep runtime staging path can still cross MAX_PATH.
            destination.rmdir()
            shutil.move(str(binary.parent.parent), str(destination))
        return destination / "program/soffice.com"
    if kind == "deb-tar":
        if not sys.platform.startswith("linux"):
            raise ValueError("LibreOffice DEB archive requires a native Linux builder")
        unpack(archive, scratch / "packages", "tar")
        packages = sorted((scratch / "packages").rglob("*.deb"))
        if not packages:
            raise ValueError("LibreOffice archive has no DEB packages")
        extracted = scratch / "deb-root"
        for package in packages:
            # dpkg-deb extracts files only; maintainer scripts are never executed.
            subprocess.run(["dpkg-deb", "--extract", str(package), str(extracted)],
                           check=True, capture_output=True)
        binary = single((extracted / "opt").glob("libreoffice*/program/soffice"), "LibreOffice program")
        shutil.copytree(binary.parent.parent, destination, symlinks=True, dirs_exist_ok=True)
        return destination / "program/soffice"
    raise ValueError(f"Unsupported LibreOffice format: {kind}")


BASH_COMMANDS = ("bash", "sh", "find", "sort", "head", "cut", "grep", "sed", "awk",
                 "cat", "mkdir", "rm", "cp", "mv", "ls", "wc", "xargs", "tr")


def stage_git_bash(archive, destination):
    """Keep the upstream MSYS tree, shell launchers, and component licenses."""
    with tarfile.open(archive) as bundle:
        for entry in bundle:
            relative = safe_relative(entry.name)
            name = relative.as_posix()
            # /proc is virtual inside MSYS, not an extractable Windows link.
            if name == "etc/mtab":
                continue
            selected = (name.startswith(("usr/", "etc/")) or
                        "license" in name.lower() or "copying" in name.lower())
            if not selected:
                continue
            target = destination.joinpath(*relative.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif entry.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(entry) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                target.chmod(entry.mode & 0o777)
            else:
                raise ValueError("Bash runtime contains an unexpected link or special file")
    required = ["usr/bin/msys-2.0.dll"]
    required += ["usr/bin/" + name + ".exe" for name in BASH_COMMANDS]
    for name in required:
        if not (destination / name).is_file():
            raise ValueError(f"Bundled Bash dependency missing: {name}")
    return destination / "usr/bin/bash.exe"


def install(manifest_path: Path, target: str, runtime: Path, executable: str) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported native tools manifest")
    runtime = runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    python_dir = runtime.joinpath(*safe_relative(executable).parent.parts)
    python_dir.mkdir(parents=True, exist_ok=True)
    installed = {}
    for name, tool in manifest.items():
        if name == "schema":
            continue
        if name not in ("officecli", "pandoc", "libreoffice", "git-bash"):
            raise ValueError(f"Unsupported native tool: {name}")
        if name == "git-bash" and not target.startswith("windows-"):
            continue
        asset = tool["targets"][target]
        with tempfile.TemporaryDirectory(prefix=".native-", dir=runtime) as folder:
            scratch = Path(folder)
            archive = scratch / "download"
            download(asset, archive)
            if name == "git-bash":
                staged = scratch / "git-bash"
                stage_git_bash(archive, staged)
                destination = runtime / "native/git-bash"
                if destination.is_symlink() or not destination.resolve().is_relative_to(runtime):
                    raise ValueError("Bash staging directory escapes the private runtime")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(staged), destination)
                binary = destination / "usr/bin/bash.exe"
            elif name == "libreoffice":
                staged = scratch / "libreoffice"
                staged.mkdir()
                binary = stage_libreoffice(archive, asset, staged, target, scratch)
                binary_relative = binary.relative_to(staged)
                destination = runtime / "native/libreoffice"
                destination.parent.mkdir(parents=True, exist_ok=True)
                # runtime is a disposable staging tree, never an activated user runtime.
                if destination.exists():
                    shutil.rmtree(destination)
                shutil.move(str(staged), destination)
                binary = destination / binary_relative
            else:
                binary_name = name + (".exe" if target.startswith("windows-") else "")
                if name == "pandoc":
                    unpack(archive, scratch / "unpacked", asset["format"])
                    staged = single((scratch / "unpacked").rglob(binary_name), "Pandoc binary")
                else:
                    staged = archive
                binary = python_dir / binary_name
                staged.chmod(0o755)
                os.replace(staged, binary)
            if not binary.is_file():
                raise ValueError(f"Bundled {name} executable missing: {binary}")
            installed[name] = {"version": tool["version"], "path": binary.relative_to(runtime).as_posix(),
                               "upstream_sha256": asset["sha256"]}
    return installed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--executable", required=True)
    args = parser.parse_args()
    tools = install(args.manifest, args.target, args.runtime, args.executable)
    (args.runtime / "native-tools.json").write_text(json.dumps(tools, indent=2) + "\n", encoding="utf-8")
    print("Verified and staged " + ", ".join(f"{name} {tool['version']}" for name, tool in tools.items()))


if __name__ == "__main__":
    main()
