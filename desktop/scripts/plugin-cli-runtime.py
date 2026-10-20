#!/usr/bin/env python3
"""Stage locked plugin CLIs alongside the existing private Playwright Node runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

VERSION = "2026.7.12-1"
PROCESS_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def member(root, relative):
    path = (root / relative).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise RuntimeError("Bundled plugin CLI file is missing")
    return path


def check(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "plugin-clis.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or manifest["openyida"]["version"] != VERSION:
        raise RuntimeError("Bundled plugin CLI version is invalid")
    node = member(root, manifest["node"])
    script = member(root, manifest["openyida"]["script"])
    utils = member(root, manifest["openyida"]["utils"])
    if digest(script) != manifest["openyida"]["sha256"]:
        raise RuntimeError("Bundled OpenYida integrity check failed")
    with tempfile.TemporaryDirectory(prefix="plugin-cli-smoke-") as temp:
        # Empty HOME and PATH ensure no developer CLI or credential can mask a
        # missing private dependency. This probe does not initiate a login.
        env = {k: v for k, v in os.environ.items()
               if k.upper() in {"SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG"}}
        env.update(HOME=temp, USERPROFILE=temp, PATH=os.defpath)
        result = subprocess.run([str(node), str(script), "agent-capabilities", "--json"],
                                cwd=temp, env=env, capture_output=True, text=True,
                                encoding="utf-8", timeout=45, check=True, **PROCESS_FLAGS)
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or data.get("openyida", {}).get("version") != VERSION:
            raise RuntimeError("OpenYida capability probe did not return JSON")
        subprocess.run([str(node), "-e", "require(process.argv[1])", str(utils)],
                       cwd=temp, env=env, capture_output=True, timeout=30, check=True, **PROCESS_FLAGS)
    return {"openyida": VERSION}


def install(root, source):
    from playwright._impl._driver import compute_driver_executable

    root, source = Path(root).resolve(), Path(source).resolve()
    node = Path(compute_driver_executable()[0]).resolve()
    node.relative_to(root)  # Must be this private runtime's Node, never the build host.
    destination = root / "native/plugin-clis"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(source / name, destination / name)
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is required on the build host to stage plugin CLIs")
    npm_path = Path(npm).resolve()
    candidates = [npm_path, npm_path.parent / "node_modules/npm/bin/npm-cli.js",
                  npm_path.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
    npm_cli = next((path for path in candidates if path.name == "npm-cli.js" and path.is_file()), None)
    if npm_cli is None:
        raise RuntimeError("The build host npm-cli.js could not be located")
    # Invoke JavaScript directly so Windows paths need no cmd.exe quoting.
    subprocess.run([str(node), str(npm_cli), "ci", "--ignore-scripts", "--no-audit", "--no-fund",
                    "--prefix", str(destination)], check=True, timeout=300, **PROCESS_FLAGS)
    package = destination / "node_modules/openyida"
    if json.loads((package / "package.json").read_text(encoding="utf-8"))["version"] != VERSION:
        raise RuntimeError("Locked OpenYida package version differs")
    script = package / "bin/yida.js"
    manifest = {"schema": 1, "node": node.relative_to(root).as_posix(),
                "lock_sha256": digest(source / "package-lock.json"),
                "openyida": {"version": VERSION, "script": script.relative_to(root).as_posix(),
                             "utils": (package / "lib/core/utils.js").relative_to(root).as_posix(),
                             "sha256": digest(script)}}
    (root / "plugin-clis.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # Bash in local conversations uses the same package and private Node as the
    # login API. Resolve relative to the shim so runtime relocation remains valid.
    binary_dir = Path(sys.executable).absolute().parent.resolve()
    binary_dir.relative_to(root)
    relative = Path(os.path.relpath(root, binary_dir)).as_posix()
    shim = binary_dir / "openyida"
    shim.write_bytes(('#!/usr/bin/env bash\n'
                    'runtime="$(cd -- "$(dirname -- "$0")/' + relative + '" && pwd)"\n'
                    'exec "$runtime/' + manifest["node"] + '" "$runtime/' +
                    manifest["openyida"]["script"] + '" "$@"\n').encode("utf-8"))
    shim.chmod(0o755)
    if b"\r" in shim.read_bytes():
        raise RuntimeError("OpenYida Bash entry must use LF newlines")
    return check(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--source")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.runtime) if args.check else install(args.runtime, args.source)))
