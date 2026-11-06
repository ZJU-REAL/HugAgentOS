#!/usr/bin/env python3
"""Publish desktop artifacts before atomically advancing platform and legacy manifests."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tarfile
import plistlib
from datetime import datetime, timezone

SLIM_PACKAGE = re.compile(r"-update[-_.]")
EXTRA_ROLES = ("runtime_archive", "runtime_manifest", "installer")
TARGET = re.compile(r"^(windows|darwin|linux)-(x86_64|aarch64|i686|armv7)$")


def version_key(version):
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Release version must be a stable X.Y.Z version")
    return tuple(map(int, version.split(".")))


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def atomic_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish_name(version, platform, sha256, artifact_name):
    """The published name: version, platform and content hash, including Mac archives.

    Signatures that cover the filename (the UOS `.deb` line) are produced against this
    name, so it is computed here and nowhere else — `--plan` hands it to the signer.
    """
    name = re.sub(r"\s+", "-", artifact_name.strip())
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Artifact filename must contain only letters, digits, dots, dashes,"
                         " underscores or spaces")
    return f"{version}-{platform}-{sha256[:16]}-{name}"


def validate_macos_archive(artifact, version):
    """Tauri must see one application root, never AppleDouble sidecars."""
    with tarfile.open(artifact, "r:gz") as archive:
        roots = set()
        for member in archive.getmembers():
            parts = member.name.rstrip("/").split("/")
            if member.name.startswith("/") or ".." in parts:
                raise ValueError("Unsafe path in macOS updater archive")
            if any(part.startswith("._") or part == "__MACOSX" for part in parts):
                raise ValueError("AppleDouble metadata in macOS updater archive; repack without macOS metadata")
            roots.add(parts[0])
        if len(roots) != 1 or not next(iter(roots), "").endswith(".app"):
            raise ValueError("macOS updater archive must contain exactly one .app root")
        app = next(iter(roots))
        try:
            info = archive.extractfile(app + "/Contents/Info.plist")
            if info is None:
                raise ValueError("Missing macOS application Info.plist")
            with info:
                metadata = plistlib.load(info)
        except KeyError as error:
            raise ValueError("Missing macOS application Info.plist") from error
        if metadata.get("CFBundleShortVersionString") != version:
            raise ValueError("macOS application version does not match release version")


def select_artifact(bundle, target, version):
    if bundle is None or not bundle.is_dir():
        raise ValueError("--bundle must be an existing artifact directory")
    if target.startswith("windows-"):
        pattern = "*-setup.exe" if list(bundle.glob("*-setup.exe")) else "*.nsis.zip"
    elif target.startswith("darwin-"):
        pattern = "*.app.tar.gz"
    else:
        # UOS ships a Debian package; other Linux desktops ship an AppImage.
        pattern = "*.deb" if list(bundle.glob("*.deb")) else "*.AppImage"
    matches = sorted(bundle.glob(pattern))
    exact = [item for item in matches if version in item.name]
    choices = exact or matches
    if len(choices) != 1:
        raise ValueError("Expected exactly one matching updater artifact")
    if target.startswith("darwin-"):
        validate_macos_archive(choices[0], version)
    return choices[0]


def select_installer(bundle, target, version):
    """The offline first-install package that sits next to a slim update package."""
    if bundle is None or not bundle.is_dir():
        raise ValueError("--installer-bundle must be an existing artifact directory")
    pattern = {"windows": "*-setup.exe", "darwin": "*.dmg"}.get(target.split("-")[0], "*.AppImage")
    matches = sorted(bundle.glob(pattern))
    exact = [item for item in matches if version in item.name]
    choices = exact or matches
    if len(choices) != 1:
        raise ValueError("Expected exactly one matching full installer")
    return choices[0]


def store_immutable(directory, name, source, sha256, label):
    destination = directory / name
    if destination.exists():
        if digest(destination) != sha256:
            if label.startswith("Runtime"):
                raise ValueError(
                    "A different runtime is already published for this fingerprint; rebuild with "
                    "HUGAGENT_RUNTIME_SOURCE pointing at this release server so the published bytes are reused")
            raise ValueError(f"Existing immutable {label.lower()} is corrupted")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=directory)
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        if digest(Path(temporary)) != sha256:
            raise ValueError(f"Copied {label.lower()} SHA-256 mismatch")
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def runtime_metadata(runtime_dir, target):
    """Describe the runtime the update package expects clients to download.

    The names must match published-runtime.mjs (build reuse) and the desktop
    client, which derive them from the target and dependency fingerprint.
    """
    manifest_path = runtime_dir / "runtime-manifest.json"
    archive = runtime_dir / "runtime-core.tar.gz"
    manifest = read_json(manifest_path)
    fingerprint = manifest.get("dependency_fingerprint", "")
    if manifest.get("target") != target:
        raise ValueError("Runtime manifest target does not match --target")
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("Runtime manifest has no dependency fingerprint")
    if not archive.is_file() or digest(archive) != manifest.get("archive_sha256"):
        raise ValueError("Runtime archive does not match runtime-manifest.json")
    stem = f"runtime-{target}-{fingerprint}"
    return {
        "fingerprint": fingerprint, "sha256": manifest["archive_sha256"],
        "size": archive.stat().st_size,
        "archive": f"{stem}.tar.gz", "manifest": f"{stem}.json",
    }, {"runtime_archive": archive, "runtime_manifest": manifest_path}


def apply_release(directory, artifact, metadata, extras=None):
    directory.mkdir(parents=True, exist_ok=True)
    platform, version = metadata["target"], metadata["version"]
    version_key(version)
    if not TARGET.fullmatch(platform):
        raise ValueError("Unsupported platform")
    if digest(artifact) != metadata["sha256"]:
        raise ValueError("Uploaded artifact SHA-256 mismatch")
    if not metadata["signature"].strip():
        raise ValueError("Missing updater signature")
    extras = extras or {}
    runtime, installer = metadata.get("runtime"), metadata.get("installer")
    if runtime and not runtime["archive"].startswith(f"runtime-{platform}-"):
        raise ValueError("Runtime does not belong to this platform")
    name = publish_name(version, platform, metadata["sha256"], artifact.name)
    with (directory / ".publish.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        index_path = directory / "release-index.json"
        if index_path.exists():
            index = read_json(index_path)
        else:
            legacy_path = directory / "latest.json"
            legacy = read_json(legacy_path) if legacy_path.exists() else None
            index = {"legacy": legacy, "latest": {}, "versions": {}}
            if legacy:
                for key, spec in legacy["platforms"].items():
                    release = {**legacy, "platforms": {key: spec}}
                    index["latest"][key] = release
                index["versions"][legacy["version"]] = legacy
        previous = index["latest"].get(platform)
        if previous and version_key(version) < version_key(previous["version"]):
            raise ValueError("Refusing to downgrade the platform release")
        release = {
            "version": version, "notes": metadata["notes"], "pub_date": metadata["pub_date"],
            # Tauri reads url/signature and ignores the rest; the UOS client additionally
            # verifies the hash it computed while downloading and shows the size.
            "platforms": {platform: {
                "url": name, "signature": metadata["signature"],
                "sha256": metadata["sha256"], "size": artifact.stat().st_size,
            }},
        }
        spec = release["platforms"][platform]
        # A slim update package carries no runtime: clients fetch `runtime.archive`
        # when the fingerprint is new. `installer` stays the offline first install.
        if runtime:
            spec["runtime"] = {key: runtime[key] for key in ("fingerprint", "sha256", "size", "archive")}
        if installer:
            spec["installer"] = installer
        candidate = index["versions"].get(version, {**release, "platforms": {}})
        old_spec = candidate["platforms"].get(platform)
        if old_spec and old_spec != release["platforms"][platform]:
            raise ValueError("This version/platform is already published with different content")
        # Content first, index last: a reader never sees a release whose
        # runtime or full installer is not yet on disk.
        if runtime:
            # Archive before manifest: build reuse treats a manifest as proof the archive exists.
            store_immutable(directory, runtime["archive"], extras["runtime_archive"],
                            runtime["sha256"], "Runtime")
            store_immutable(directory, runtime["manifest"], extras["runtime_manifest"],
                            digest(extras["runtime_manifest"]), "Runtime manifest")
        if installer:
            store_immutable(directory, installer["url"], extras["installer"],
                            installer["sha256"], "Full installer")
        store_immutable(directory, name, artifact, metadata["sha256"], "Artifact")
        index["latest"][platform] = release
        candidate["platforms"][platform] = release["platforms"][platform]
        index["versions"][version] = candidate
        required = set(index["latest"]) | set(index.get("required_targets", [])) | set(metadata["required_targets"])
        index["required_targets"] = sorted(required)
        legacy = index["legacy"]
        eligible = [
            item for item in index["versions"].values()
            if required.issubset(item["platforms"])
            and (legacy is None or version_key(item["version"]) >= version_key(legacy["version"]))
        ]
        if eligible:
            index["legacy"] = max(eligible, key=lambda item: version_key(item["version"]))
        # One authoritative file: readers see either the previous or the complete new state.
        atomic_json(index_path, index)
        if index["legacy"]:
            try:
                atomic_json(directory / "latest.json", index["legacy"])
            except OSError as error:
                print(f"Release committed; compatibility mirror needs retry: {error}", file=sys.stderr)
        print(json.dumps({
            "platform": platform, "version": version, "filename": name,
            "sha256": metadata["sha256"],
            "legacy_version": (index["legacy"] or {}).get("version"),
            "legacy_platforms": sorted((index["legacy"] or {}).get("platforms", {})),
        }))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--target", default="windows-x86_64")
    parser.add_argument("--notes", default="")
    parser.add_argument("--required-target", action="append", default=[])
    parser.add_argument("--ssh")
    parser.add_argument("--ssh-port", default="22")
    parser.add_argument("--container", default="hugagent-backend")
    parser.add_argument("--release-dir", default="/app/storage/desktop_release")
    parser.add_argument("--local-dir", type=Path, help="Publish directly to a local release directory")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--plan", action="store_true",
                        help="Report the published filename and hash so a signer can sign them")
    parser.add_argument("--runtime-dir", type=Path,
                        help="Directory holding runtime-core.tar.gz and runtime-manifest.json "
                             "(desktop/generated) when --bundle is a slim update package")
    parser.add_argument("--installer-bundle", type=Path,
                        help="Bundle directory of the full offline installer for first installs")
    parser.add_argument("--apply", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--artifact", type=Path, help=argparse.SUPPRESS)
    for role in EXTRA_ROLES:
        parser.add_argument(f"--{role.replace('_', '-')}", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.apply:
        extras = {role: getattr(args, role) for role in EXTRA_ROLES if getattr(args, role)}
        apply_release(Path(args.release_dir), args.artifact, read_json(args.apply), extras)
        return
    if not args.version:
        args.version = read_json(Path(__file__).resolve().parents[1] / "src-tauri/tauri.conf.json")["version"]
    version_key(args.version)
    if any(not TARGET.fullmatch(target) for target in [args.target, *args.required_target]):
        raise ValueError("Unsupported target")
    artifact = select_artifact(args.bundle, args.target, args.version)
    if args.plan:
        # Signing precedes publication, so the signer needs the published name up front.
        checksum = digest(artifact)
        print(json.dumps({
            "artifact": str(artifact), "filename": publish_name(
                args.version, args.target, checksum, artifact.name),
            "sha256": checksum, "size": artifact.stat().st_size,
            "version": args.version, "target": args.target,
        }))
        return
    metadata = {
        "version": args.version, "target": args.target, "notes": args.notes,
        "pub_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signature": Path(str(artifact) + ".sig").read_text().strip(),
        "sha256": digest(artifact), "required_targets": args.required_target,
    }
    # Slim update packages (named *-update*) carry an empty runtime placeholder,
    # so the runtime they reference must be published with them.
    slim = SLIM_PACKAGE.search(artifact.name) is not None
    if slim != bool(args.runtime_dir):
        raise ValueError("A slim *-update* package must be published with --runtime-dir, and only it"
                         if slim else "--runtime-dir is only for slim *-update* packages")
    extras = {}
    if args.runtime_dir:
        metadata["runtime"], runtime_files = runtime_metadata(args.runtime_dir, args.target)
        extras.update(runtime_files)
    if args.installer_bundle:
        installer = select_installer(args.installer_bundle, args.target, args.version)
        checksum = digest(installer)
        metadata["installer"] = {
            "url": publish_name(args.version, args.target, checksum, installer.name),
            "sha256": checksum, "size": installer.stat().st_size,
        }
        extras["installer"] = installer
    if args.dry_run:
        # Simulate against a copy of manifest state; never mutate the destination.
        with tempfile.TemporaryDirectory(prefix="desktop-dry-run-") as temporary:
            directory = Path(temporary)
            if args.local_dir:
                for filename in ("release-index.json", "latest.json"):
                    source = args.local_dir / filename
                    if source.exists():
                        shutil.copyfile(source, directory / filename)
            apply_release(directory, artifact, metadata, extras)
        return
    if args.local_dir:
        if args.ssh:
            raise ValueError("--local-dir and --ssh are mutually exclusive")
        apply_release(args.local_dir, artifact, metadata, extras)
        return

    def command(parts, **kwargs):
        cmd = ["ssh", "-p", args.ssh_port, args.ssh, shlex.join(parts)] if args.ssh else parts
        return subprocess.run(cmd, check=True, **kwargs)

    with tempfile.TemporaryDirectory(prefix="desktop-publish-") as temporary:
        stage = Path(temporary)
        shutil.copyfile(__file__, stage / "publisher.py")
        (stage / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        # Keep artifacts off the archive copy; tar reads the original bundle directly.
        remote_stage = "/tmp/" + stage.name
        docker = ["docker", "exec", "-u", "0", args.container]
        command([*docker, "mkdir", "-p", remote_stage])
        try:
            def transfer(directory, names, remote_dir):
                tar = subprocess.Popen(["tar", "-cf", "-", "-C", str(directory), *names],
                                       stdout=subprocess.PIPE)
                try:
                    command(["docker", "exec", "-i", "-u", "0", args.container,
                             "tar", "-xf", "-", "-C", remote_dir], stdin=tar.stdout)
                finally:
                    tar.stdout.close()
                    code = tar.wait()
                if code:
                    raise RuntimeError("Artifact transfer failed")

            transfer(stage, ["publisher.py", "metadata.json"], remote_stage)
            transfer(artifact.parent.resolve(), [artifact.name], remote_stage)
            apply_args = []
            # One directory per role: the full installer may share the update package's name.
            for role, path in extras.items():
                remote_dir = f"{remote_stage}/{role}"
                command([*docker, "mkdir", "-p", remote_dir])
                transfer(path.parent.resolve(), [path.name], remote_dir)
                apply_args += [f"--{role.replace('_', '-')}", f"{remote_dir}/{path.name}"]
            command([*docker, "python", remote_stage + "/publisher.py",
                     "--apply", remote_stage + "/metadata.json",
                     "--artifact", remote_stage + "/" + artifact.name,
                     "--release-dir", args.release_dir, *apply_args])
        finally:
            command([*docker, "rm", "-rf", remote_stage])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Desktop publication failed: {error}", file=sys.stderr)
        sys.exit(1)
