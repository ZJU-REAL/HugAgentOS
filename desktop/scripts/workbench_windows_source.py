"""Complete source receipts; never include private configuration or runtime data."""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath


def excluded(path):
    parts = PurePosixPath(path).parts
    name = parts[-1]
    return (parts[0] in {"storage", "data"}
            or any(part in {".git", ".scratch", ".worktrees", ".venv", "venv", "node_modules",
                           "target", "dist", "build", "generated", "__pycache__", ".tauri",
                           ".config", ".cache", ".pytest_cache", ".ssh", "credentials", "secrets"} for part in parts)
            or (name.startswith(".env") and not name.endswith(".example"))
            or name in {"auth.json", "server.json", "credentials.json", "token.json", ".npmrc", ".pypirc", ".netrc", ".git-credentials"}
            or name.lower().endswith((".key", ".pem", ".p12", ".pfx", ".keychain-db",
                                      ".db", ".sqlite", ".sqlite3", ".sqlite-wal", ".sqlite-shm")))


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_hash(files):
    return hashlib.sha256(json.dumps(files, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root).decode()


def source_snapshot(root):
    root = root.resolve(strict=True)
    if Path(git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
        raise ValueError("SourceRoot must be the absolute Git repository root")
    index_modes = {entry.split("\t", 1)[1]: entry.split(" ", 1)[0]
                   for entry in git(root, "ls-files", "--stage", "-z").split("\0") if entry}
    paths = sorted(set(filter(None, git(root, "ls-files", "-co", "--exclude-standard", "-z").split("\0"))))
    files = []
    for relative in paths:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError("Unsafe Git source path")
        if excluded(relative):
            continue
        path = root / relative
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError("Source parent escapes checkout: " + relative)
        if path.is_symlink():
            target = os.readlink(path)
            if not path.resolve().is_relative_to(root):
                raise ValueError("Source symlink escapes checkout: " + relative)
            files.append({"path": relative, "kind": "symlink", "mode": "120000", "target": target})
        elif not path.exists():
            files.append({"path": relative, "deleted": True, "mode": index_modes.get(relative)})
        elif path.is_file():
            mode = ("100755" if path.stat().st_mode & 0o111 else "100644") if os.name != "nt" else index_modes.get(relative, "100644")
            files.append({"path": relative, "kind": "file", "mode": mode,
                          "size": path.stat().st_size, "sha256": digest(path)})
        else:
            raise ValueError("Unsupported source kind: " + relative)
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return {"files": files, "sourceTreeHash": tree_hash(files),
            "commit": git(root, "rev-parse", "HEAD").strip(), "dirty": bool(status),
            "dirtyStatusSha256": hashlib.sha256(status.encode()).hexdigest(),
            "sourceScope": "all Git tracked and nonignored untracked source; explicit mode/tombstone; secrets/data/build outputs excluded"}


def verify(root, receipt):
    if tree_hash(receipt["files"]) != receipt["sourceTreeHash"]:
        raise ValueError("Source receipt tree hash mismatch")
    actual = source_snapshot(root)
    if actual["commit"] != receipt["commit"] or actual["sourceTreeHash"] != receipt["sourceTreeHash"]:
        expected = {item["path"]: item for item in receipt["files"]}
        present = {item["path"]: item for item in actual["files"]}
        changed = sorted(path for path in expected.keys() | present.keys() if expected.get(path) != present.get(path))
        raise ValueError("Frozen source differs (files/modes/tombstones/baseline): " + ", ".join(changed[:20]))
    return actual


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    verified = verify(args.root, json.loads(args.receipt.read_text(encoding="utf-8-sig")))
    print(json.dumps({"verified": True, "sourceTreeHash": verified["sourceTreeHash"],
                      "files": len(verified["files"]), "baseline": verified["commit"]}))
